/**
 * webOnboarding.js -- where the website keeps the user's work, asked at the
 * first moment it has something to keep.
 *
 * Plexora Lite used to ask where to save projects the moment its Python had
 * started, on whatever page loaded first, in a two-button question nobody had
 * a reason to answer yet; a cancelled folder dialog then recorded "browser"
 * for good. Now nothing is asked on load. The first action that writes
 * something to keep -- an image picked to open (services/pickedFiles.js
 * `gate`, from browsePicker.js), a server saved (connectionModal.js) -- awaits
 * `ensure()`, which shows this dialog only when no place has been chosen yet,
 * or when the chosen folder is not open this visit. The action then carries
 * on ("Continue to my image"), or stops with nothing left behind (the X).
 *
 *   ensure({reason})       -> "ready" | "cancelled"; at once "ready" when a
 *                             place is chosen and open (and on any page a
 *                             Plexora served: there is nothing to set up)
 *   open({reason, start})  -> the same dialog shown whatever was chosen
 *                             ("Set up again" in Settings); start "welcome"
 *   reason: "pick" (an image waits: its button says so), "remote", "save",
 *           "settings", "home"
 *
 * The screens, one at a time in one native <dialog> (focus held inside it,
 * Escape and the X both close it, which is a cancel):
 *   welcome      Welcome to Plexora Lite: used it before / new to it
 *   returning    Welcome back: locate the folder, or set up a new workspace
 *   found        the folder is a workspace (or holds the one an old version
 *                of the site made inside it): its projects and figures
 *   unrecognised the folder is not one: choose another, set it up, or keep
 *                the work in the browser
 *   home         Give your work a home: a folder (recommended) or the browser
 *   create       Create your Plexora folder: Documents › Plexora, step by step
 *   existing / foreign   a folder picked to create in already holds Plexora
 *                data / other files
 *   setup        the work in progress, then "Your workspace is ready" (or
 *                "Browser storage is ready"), with where, and a little confetti
 *   reconnect    We couldn't reconnect to your Plexora folder
 *   cancelled, denied, unsupported, not-writable, quota, partial,
 *   projects-inside   what went wrong, and the ways on from it
 *
 * Everything that touches storage is the host page's (services/
 * browserStore.js, `window.parent.PlexoraStore`): the folder dialog is opened
 * from the click on a button here, as the first thing the click awaits, so
 * the browser lets it open (a click in this frame counts for its parent).
 * Nothing here needs Python; a first visit's folder waits for Python to start
 * on the last step, behind its progress line.
 *
 * Classic script, `window.PlexoraOnboarding`. Loaded on the website only
 * (base.html), and inert anywhere without the host's PlexoraStore. Its CSS is
 * main.css's `.plx-onb` block: the app shell turns page stylesheets off.
 */
(function (global) {
    "use strict";

    const doc = global.document;
    const DOWNLOAD = "https://github.com/nirmallab/plexora/releases/latest";
    const STEPS = 3;
    const CONFETTI = 14;
    const CONFETTI_MS = 900;
    const SVG_NS = "http://www.w3.org/2000/svg";

    // -- the host's storage ---------------------------------------------------

    function host() {
        try {
            const parent = global.parent;
            if (parent && parent !== global && parent.PlexoraStore) return parent;
        } catch (error) { /* another origin */ }
        return global.PlexoraStore ? global : null;
    }

    const storeOf = () => (host() ? host().PlexoraStore : null);
    const reducedMotion = () => Boolean(global.matchMedia
                                        && global.matchMedia("(prefers-reduced-motion: reduce)").matches);

    // -- icons ----------------------------------------------------------------
    //
    // One family: a 24-unit grid, 1.75 strokes with round joins, stroked in
    // the Lite gradient (cyan to violet, `plx-onb-ink`) and washed underneath
    // in a faint tint of it (`.d`, `plx-onb-wash`) where they stand in a tile
    // (main.css). Inline, so they draw without Font Awesome and in the
    // dialog's own colours.

    const FOLDER = "M3 7.5a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.4.6l1.3 1.3a2 2 0 0 0 1.4.6H19a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z";
    const folder = (extra = "") => `<path class="d" d="${FOLDER}"/><path d="${FOLDER}"/>${extra}`;
    const SHIELD = "M12 3 5 5.8v5.4c0 4.3 2.9 7.6 7 9.3 4.1-1.7 7-5 7-9.3V5.8Z";
    const WINDOW = '<rect class="d" x="3" y="4" width="18" height="16" rx="2.5"/><rect x="3" y="4" width="18" height="16" rx="2.5"/>'
        + '<path d="M3 9h18"/><path class="dot" d="M6.5 6.5h.01"/><path class="dot" d="M9 6.5h.01"/>';
    const ICONS = {
        layers: '<path class="d" d="m12 3 9 5-9 5-9-5Z"/><path d="m12 3 9 5-9 5-9-5Z"/><path d="m3 12.5 9 5 9-5"/>'
            + '<path d="m3 16.5 9 5 9-5"/>',
        sparkle: '<path class="d" d="M11 3.5 12.9 9l5.6 1.9-5.6 1.9L11 18.5l-1.9-5.7-5.6-1.9L9.1 9Z"/>'
            + '<path d="M11 3.5 12.9 9l5.6 1.9-5.6 1.9L11 18.5l-1.9-5.7-5.6-1.9L9.1 9Z"/><path d="M19 3v4"/><path d="M17 5h4"/>',
        history: '<path class="d" d="M10.5 20H5a2 2 0 0 1-2-2V7.5a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.4.6l1.3 1.3a2 2 0 0 0 1.4.6H19a2 2 0 0 1 2 2v1"/>'
            + '<path d="M10.5 20H5a2 2 0 0 1-2-2V7.5a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.4.6l1.3 1.3a2 2 0 0 0 1.4.6H19a2 2 0 0 1 2 2v1"/>'
            + '<circle class="d" cx="17" cy="16.5" r="4.5"/><circle cx="17" cy="16.5" r="4.5"/><path d="M17 14.5v2l1.3 1"/>',
        folder: folder(),
        folderSearch: '<path class="d" d="M10.5 20H5a2 2 0 0 1-2-2V7.5a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.4.6l1.3 1.3a2 2 0 0 0 1.4.6H19a2 2 0 0 1 2 2v1.5"/>'
            + '<path d="M10.5 20H5a2 2 0 0 1-2-2V7.5a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.4.6l1.3 1.3a2 2 0 0 0 1.4.6H19a2 2 0 0 1 2 2v1.5"/>'
            + '<circle cx="16.5" cy="16" r="3"/><path d="m21 20.5-2.3-2.3"/>',
        folderPlus: folder('<path d="M12 10.5v6"/><path d="M9 13.5h6"/>'),
        folderCheck: folder('<path d="m8.8 13.6 2.3 2.3 4.3-4.6"/>'),
        folderX: folder('<path d="m10 11.5 4 4"/><path d="m14 11.5-4 4"/>'),
        folderQuestion: folder('<path d="M10.2 11.6a1.9 1.9 0 1 1 2.6 1.8c-.5.2-.8.6-.8 1.1v.3"/><path class="dot" d="M12 17.1h.01"/>'),
        folderUp: folder('<path d="M12 17v-5.5"/><path d="m9.5 14 2.5-2.5 2.5 2.5"/>'),
        folderDown: folder('<path d="M12 10.5V16"/><path d="m9.5 13.5 2.5 2.5 2.5-2.5"/>'),
        folderFiles: folder('<path d="M8 12.5h8"/><path d="M8 15.5h5"/>'),
        home: '<path class="d" d="M5 10v9a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-9l-7-6Z"/><path d="M3 11.5 12 4l9 7.5"/>'
            + '<path d="M5 10v9a1 1 0 0 0 1 1h3.5v-5.5h5V20H18a1 1 0 0 0 1-1v-9"/>',
        monitor: '<rect class="d" x="3" y="4" width="18" height="12.5" rx="2"/><rect x="3" y="4" width="18" height="12.5" rx="2"/>'
            + '<path d="M8.5 20.5h7"/><path d="M12 16.5v4"/>',
        browser: WINDOW,
        browserCheck: WINDOW + '<path d="m9 14.6 2 2 4-4.2"/>',
        browserOff: '<path d="M7.5 4H18.5A2.5 2.5 0 0 1 21 6.5V17"/><path d="M19.6 19.6A2.5 2.5 0 0 1 18.5 20h-13A2.5 2.5 0 0 1 3 17.5v-11c0-.5.2-1 .4-1.4"/>'
            + '<path d="M12 9h9"/><path d="M3 9h3"/><path d="m2.5 2.5 19 19"/>',
        server: '<rect class="d" x="3.5" y="3.5" width="17" height="7" rx="2"/><rect class="d" x="3.5" y="13.5" width="17" height="7" rx="2"/>'
            + '<rect x="3.5" y="3.5" width="17" height="7" rx="2"/><rect x="3.5" y="13.5" width="17" height="7" rx="2"/>'
            + '<path class="dot" d="M7.5 7h.01"/><path class="dot" d="M7.5 17h.01"/><path d="M11 7h5.5"/><path d="M11 17h5.5"/>',
        gate: '<path d="M4 4v15a1 1 0 0 0 1 1h15"/><path class="d" d="m9 7.5 6.5-1.5 3 5.5-4.5 4.5-5.5-2.5Z"/>'
            + '<path d="m9 7.5 6.5-1.5 3 5.5-4.5 4.5-5.5-2.5Z"/><path class="dot" d="M7.5 17h.01"/>'
            + '<path class="dot" d="M12 10.5h.01"/><path class="dot" d="M14.5 12.5h.01"/><path class="dot" d="M18 17h.01"/>',
        shieldCheck: `<path class="d" d="${SHIELD}"/><path d="${SHIELD}"/><path d="m9 12 2.2 2.2L15.5 10"/>`,
        shieldX: `<path class="d" d="${SHIELD}"/><path d="${SHIELD}"/><path d="m10 10 4 4"/><path d="m14 10-4 4"/>`,
        plug: '<path class="d" d="M6.5 8h11v3.5a5.5 5.5 0 0 1-11 0Z"/><path d="M6.5 8h11v3.5a5.5 5.5 0 0 1-11 0Z"/>'
            + '<path d="M9.5 8V3.5"/><path d="M14.5 8V3.5"/><path d="M12 17v4"/>',
        check: '<path d="M20 6 9 17l-5-5"/>',
        pen: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
        clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        folders: '<path d="M20 17a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3.9a2 2 0 0 1-1.7-.9l-.8-1.2a2 2 0 0 0-1.7-.9H8a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2Z"/><path d="M2 8v11a2 2 0 0 0 2 2h14"/>',
        warning: '<path class="d" d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>'
            + '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path class="dot" d="M12 17h.01"/>',
        lock: '<rect class="d" x="4" y="10.5" width="16" height="10.5" rx="2.5"/><rect x="4" y="10.5" width="16" height="10.5" rx="2.5"/>'
            + '<path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/><path d="M12 15v2"/>',
        disk: '<ellipse class="d" cx="12" cy="5.5" rx="8" ry="3"/><ellipse cx="12" cy="5.5" rx="8" ry="3"/>'
            + '<path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
        arrowLeft: '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>',
        arrowRight: '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
        chevron: '<path d="m9 18 6-6-6-6"/>',
        close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
        external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    };

    //: `draw`: every stroke given a length of 1, so CSS can draw it on
    //: (stroke-dashoffset 1 to 0) with no measuring: a screen's head icon.
    //: Not the wash under it, nor a dot.
    function svg(name, draw = false) {
        let inner = ICONS[name] || "";
        if (draw) inner = inner.replace(/<(path|circle|rect|ellipse) (?!class=)/g, '<$1 pathLength="1" ');
        return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" `
            + `stroke-linecap="round" stroke-linejoin="round" focusable="false">${inner}</svg>`;
    }

    //: What Plexora is, on the welcome screen: four things that are true of
    //: the website. Images are read in the page (services/pickedFiles.js,
    //: browserPyramid.js; magic select runs in it, onnxExecutor.js); the AI
    //: features send the views they reason over to the AI gateway
    //: (plexora/ai/harness/decision.py `image_block`), hence the second half of
    //: the first line. Servers: SSH through the relay starts a data node there
    //: (services/webRemote.js), or a node is paired (connectorClient.js).
    //: Formats: the home page's own examples (browsePicker.js KIND_EXAMPLES);
    //: spatial transcriptomics is left out, being a licence's. The tools are
    //: the free tier's (plexora/licensing/manifest.py).
    const FEATURES = [
        ["shieldCheck", "Your images are not uploaded",
         "They are read in this browser. AI features, when used, send only the views they need."],
        ["server", "Work where your data lives",
         "Connect to a server or HPC cluster and open its images there, without copying them."],
        ["layers", "Multiplexed and whole-slide",
         "OME-TIFF, OME-Zarr, DICOM and .svs, with many channels at once."],
        ["gate", "Analysis in one place",
         "Gating, QC, regions, cell exploration and figures."],
    ];

    // -- building -------------------------------------------------------------

    function el(tag, className, text) {
        const node = doc.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function icon(name, className = "plx-onb-icon") {
        const node = el("span", className);
        node.setAttribute("aria-hidden", "true");
        node.innerHTML = svg(name);
        return node;
    }

    //: Text with **bold** runs, as nodes (never markup: names come from the
    //: user's own folders).
    function rich(tag, className, text) {
        const node = el(tag, className);
        String(text).split(/(\*\*[^*]+\*\*)/).forEach((part) => {
            if (!part) return;
            if (part.startsWith("**") && part.endsWith("**")) node.appendChild(el("strong", null, part.slice(2, -2)));
            else node.appendChild(doc.createTextNode(part));
        });
        return node;
    }

    const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

    let serial = 0;
    const uid = (stem) => `plx-onb-${stem}-${++serial}`;

    function button({ label, kind = "secondary", iconName = null, trailing = null, onClick, role = null }) {
        const classes = { primary: "plx-button plx-button-primary plx-onb-primary",
                          secondary: "plx-button plx-onb-secondary",
                          link: "plx-onb-link",
                          back: "plx-onb-back" };
        const node = el("button", classes[kind] || classes.secondary);
        node.type = "button";
        if (role) node.dataset.role = role;
        if (iconName) node.appendChild(icon(iconName, "plx-onb-button-icon"));
        node.appendChild(el("span", "plx-onb-button-label", label));
        if (trailing) node.appendChild(icon(trailing, "plx-onb-button-icon is-trailing"));
        node.addEventListener("click", (event) => {
            event.preventDefault();
            if (node.disabled) return;
            onClick(event);
        });
        return node;
    }

    //: A choice drawn as a card: an icon, a title, a line or two.
    function card({ name, iconName, title, text, chip = null, notes = null, onClick, recommended = false,
                    quiet = false }) {
        const node = el("button", `plx-onb-card${recommended ? " is-recommended" : ""}${quiet ? " is-quiet" : ""}`);
        node.type = "button";
        node.dataset.choice = name;
        const titleId = uid("card");
        node.setAttribute("aria-labelledby", titleId);
        node.appendChild(icon(iconName, "plx-onb-card-icon"));
        const words = el("span", "plx-onb-card-words");
        const head = el("span", "plx-onb-card-head");
        head.appendChild(el("span", "plx-onb-card-title", title)).id = titleId;
        if (chip) head.appendChild(el("span", "plx-onb-chip", chip));
        words.appendChild(head);
        if (text) {
            const line = el("span", "plx-onb-card-text", text);
            line.id = uid("card-text");
            node.setAttribute("aria-describedby", line.id);
            words.appendChild(line);
        }
        if (notes) {
            const list = el("span", "plx-onb-card-notes");
            notes.forEach((note) => list.appendChild(el("span", "plx-onb-card-note", note)));
            words.appendChild(list);
        }
        node.appendChild(words);
        node.appendChild(icon("chevron", "plx-onb-card-go"));
        node.addEventListener("click", (event) => {
            event.preventDefault();
            if (!node.disabled) onClick(event);
        });
        return node;
    }

    //: A workspace's contents, as the user knows them.
    function summary(look) {
        const box = el("div", "plx-onb-summary");
        box.appendChild(icon("folder", "plx-onb-summary-icon"));
        const words = el("div", "plx-onb-summary-words");
        words.appendChild(el("p", "plx-onb-summary-name", look.label || look.name));
        const counts = [plural(look.projects.length, "project", "projects")];
        if (look.figures) counts.push(plural(look.figures, "figure", "figures"));
        if (look.captures) counts.push("bookmarks and captures");
        words.appendChild(el("p", "plx-onb-summary-meta", counts.join(" · ")));
        if (look.projects.length) {
            const list = el("ul", "plx-onb-summary-projects");
            list.setAttribute("aria-label", "Projects in this folder");
            look.projects.slice(0, 4).forEach((project) => list.appendChild(el("li", "plx-onb-pill", project.title)));
            if (look.projects.length > 4) list.appendChild(el("li", "plx-onb-pill is-more", `${look.projects.length - 4} more`));
            words.appendChild(list);
        }
        box.appendChild(words);
        return box;
    }

    //: Documents › Plexora as a file window would show it: what the create
    //: screen's steps lead to.
    function folderTree() {
        const box = el("div", "plx-onb-tree");
        box.setAttribute("aria-hidden", "true");
        const bar = el("div", "plx-onb-tree-bar");
        bar.appendChild(el("span", "plx-onb-tree-dot"));
        bar.appendChild(el("span", "plx-onb-tree-dot"));
        bar.appendChild(el("span", "plx-onb-tree-dot"));
        bar.appendChild(el("span", "plx-onb-tree-title", "Choose a folder"));
        box.appendChild(bar);
        const rows = el("div", "plx-onb-tree-rows");
        [["Documents", "is-parent"], ["Papers", "is-child"], ["Research", "is-child"],
         ["Plexora", "is-child is-target"]].forEach(([name, kind]) => {
            const row = el("div", `plx-onb-tree-row ${kind}`);
            row.appendChild(icon("folder", "plx-onb-tree-icon"));
            row.appendChild(el("span", "plx-onb-tree-name", name));
            if (kind.includes("is-target")) row.appendChild(el("span", "plx-onb-tree-tag", "New folder"));
            rows.appendChild(row);
        });
        box.appendChild(rows);
        return box;
    }

    //: A place, as the success screen names it.
    function place(result) {
        const box = el("div", "plx-onb-place");
        const folder = result.kind === "folder";
        box.appendChild(icon(folder ? "folder" : "browser", "plx-onb-place-icon"));
        const words = el("div", "plx-onb-place-words");
        words.appendChild(el("span", "plx-onb-place-caption", folder ? "Folder on this computer" : "Storage"));
        words.appendChild(el("span", "plx-onb-place-name", folder ? result.label : "This browser"));
        box.appendChild(words);
        return box;
    }

    function confetti() {
        const burst = el("span", "plx-onb-confetti");
        burst.setAttribute("aria-hidden", "true");
        const colours = ["#22d3ee", "#a78bfa", "#67e8f9", "#c4b5fd", "#34d399", "#e0f2fe", "#c084fc"];
        for (let i = 0; i < CONFETTI; i += 1) {
            const angle = (i / CONFETTI) * Math.PI * 2 + (i % 3) * 0.21;
            const reach = 46 + ((i * 37) % 30);
            const bit = el("i", i % 3 === 0 ? "is-dot" : "is-strip");
            bit.style.setProperty("--x", `${Math.round(Math.cos(angle) * reach)}px`);
            bit.style.setProperty("--y", `${Math.round(Math.sin(angle) * reach - 12)}px`);
            bit.style.setProperty("--r", `${(i * 67) % 360}deg`);
            bit.style.setProperty("--c", colours[i % colours.length]);
            bit.style.setProperty("--d", `${(i % 4) * 25}ms`);
            burst.appendChild(bit);
        }
        return burst;
    }

    // -- the dialog -----------------------------------------------------------

    function shell(onClose) {
        const dialog = el("dialog", "plx-dialog plx-import plx-onb");
        dialog.dataset.phase = "idle";
        // The gradients every tile's icon is drawn with, from the Lite palette
        // (main.css `--plx-lite-cyan`, `--plx-lite-violet`): the stroke, and
        // the faint wash under it. In user space: a straight line has no box
        // for a bounding-box gradient to fill.
        const defs = doc.createElementNS(SVG_NS, "svg");
        defs.setAttribute("class", "plx-onb-defs");
        defs.setAttribute("aria-hidden", "true");
        defs.setAttribute("focusable", "false");
        const ramp = (id, opacity) => `<linearGradient id="${id}" gradientUnits="userSpaceOnUse" x1="3" y1="3" x2="21" y2="21">`
            + `<stop offset="0" style="stop-color: var(--plx-lite-cyan); stop-opacity: ${opacity}"/>`
            + `<stop offset="1" style="stop-color: var(--plx-lite-violet); stop-opacity: ${opacity}"/></linearGradient>`;
        defs.innerHTML = `<defs>${ramp("plx-onb-ink", 1)}${ramp("plx-onb-wash", 0.2)}</defs>`;
        dialog.appendChild(defs);

        const head = el("header", "plx-onb-head");
        const brand = el("p", "plx-onb-brand");
        const logo = el("img", "plx-onb-logo");
        logo.alt = "";
        const navbarLogo = doc.getElementById("navbar-logo");
        logo.src = (navbarLogo && (navbarLogo.currentSrc || navbarLogo.src))
            || (typeof global.plexoraUrl === "function" ? global.plexoraUrl("client/src/img/logo.svg") : "");
        if (logo.src) brand.appendChild(logo);
        brand.appendChild(el("span", "plx-onb-wordmark", "Plexora"));
        const version = el("span", "plx-onb-version");
        version.hidden = true;
        brand.appendChild(version);
        head.appendChild(brand);

        // How far along, said and drawn: a short setup should look short.
        const steps = el("div", "plx-onb-steps-at");
        const stepLabel = el("span", "plx-onb-step-label");
        stepLabel.setAttribute("aria-hidden", "true");
        steps.appendChild(stepLabel);
        const progress = el("ol", "plx-onb-progress");
        for (let i = 1; i <= STEPS; i += 1) {
            const dot = el("li", "plx-onb-dot");
            dot.appendChild(el("span", "plx-onb-sr", `Step ${i}`));
            progress.appendChild(dot);
        }
        // The step: a pill that slides along the dots to the one now.
        const track = el("div", "plx-onb-track");
        track.appendChild(progress);
        const thumb = el("span", "plx-onb-dot-thumb");
        thumb.setAttribute("aria-hidden", "true");
        track.appendChild(thumb);
        steps.appendChild(track);
        head.appendChild(steps);

        const close = el("button", "plx-picker-close plx-onb-close");
        close.type = "button";
        close.title = "Close";
        close.setAttribute("aria-label", "Close");
        close.innerHTML = svg("close");
        close.addEventListener("click", () => onClose());
        head.appendChild(close);
        dialog.appendChild(head);

        const stage = el("div", "plx-onb-stage");
        dialog.appendChild(stage);
        const live = el("p", "plx-onb-sr");
        live.setAttribute("role", "status");
        live.setAttribute("aria-live", "polite");
        dialog.appendChild(live);

        // The site's version, which the host knows once Python has started:
        // in the header, and under the welcome screen's name (`versioned`).
        const versions = [version];
        let known = "";
        const say = (text) => {
            known = text || "";
            versions.forEach((node) => {
                node.textContent = known ? `Version ${known}` : "";
                node.hidden = !known;
            });
        };
        const site = host() && host().PlexoraSite;
        if (site && site.version) {
            say(site.version);
        } else if (host() && host().PlexoraShellStarted) {
            Promise.resolve(host().PlexoraShellStarted).then(() => say(host().PlexoraSite && host().PlexoraSite.version))
                .catch(() => null);
        }
        const versioned = (node) => {
            versions.push(node);
            node.textContent = known ? `Version ${known}` : "";
            node.hidden = !known;
            return node;
        };
        return { dialog, stage, progress, track, thumb, stepLabel, steps, close, live, version, versioned,
                 logo: logo.src };
    }

    /**
     * One run of the dialog: its screens, what it knows so far, and the one
     * answer it settles with when it closes ("ready" or "cancelled").
     */
    function run({ reason = "save", start = "welcome" } = {}) {
        const store = storeOf();
        const ctx = {
            reason,
            handle: null,
            look: null,
            path: start === "reconnect" ? "reconnect" : null,
            retry: null,
            error: null,
            step: 1,
            result: null,
        };
        let answer = "cancelled";
        let busy = false;
        let current = null;
        const history = [];
        const ui = shell(() => {
            if (!busy) ui.dialog.close();
        });

        function setBusy(on) {
            busy = on;
            ui.dialog.dataset.phase = on ? "working" : "idle";
            ui.close.disabled = on;
            if (current) current.node.querySelectorAll("button").forEach((node) => {
                if (on) {
                    node.dataset.wasDisabled = node.disabled ? "1" : "";
                    node.disabled = true;
                } else {
                    node.disabled = node.dataset.wasDisabled === "1";
                }
            });
        }

        function setStep(step, done = false) {
            ui.steps.hidden = step === null;
            ui.stepLabel.textContent = done ? "Done" : step === null ? "" : `Step ${step} of ${STEPS}`;
            ui.thumb.style.setProperty("--at", String(Math.max(0, (step || 1) - 1)));
            ui.track.dataset.done = done ? "true" : "false";
            const dots = Array.from(ui.progress.children);
            dots.forEach((dot, index) => {
                const at = index + 1;
                dot.className = `plx-onb-dot${done || at < step ? " is-done" : ""}${!done && at === step ? " is-active" : ""}`;
                if (!done && at === step) dot.setAttribute("aria-current", "step");
                else dot.removeAttribute("aria-current");
            });
            ui.progress.setAttribute("aria-label", done ? "All steps done" : `Step ${step} of ${STEPS}`);
        }

        // -- moving between screens --------------------------------------

        function show(name, { back = false } = {}) {
            const def = SCREENS[name](ctx);
            if (!back && current && current.name !== name && !current.def.transient) history.push(current.name);
            if (def.step !== undefined) ctx.step = def.step;
            setStep(def.step === undefined ? ctx.step : def.step);
            const node = render(name, def);
            const previous = current;
            current = { name, def, node };
            ui.dialog.dataset.screen = name;
            ui.stage.appendChild(node);
            if (previous) {
                const moving = !reducedMotion();
                const from = ui.stage.offsetHeight;
                previous.node.classList.add("is-leaving", ...(back ? ["is-leaving-back"] : []));
                node.classList.add(back ? "is-entering-back" : "is-entering");
                if (moving) {
                    ui.stage.style.height = `${from}px`;
                    void ui.stage.offsetHeight;
                    ui.stage.style.height = `${node.offsetHeight}px`;
                    ui.stage.classList.add("is-moving");
                    global.setTimeout(() => {
                        previous.node.remove();
                        ui.stage.style.height = "";
                        ui.stage.classList.remove("is-moving");
                        node.classList.remove("is-entering", "is-entering-back");
                    }, 320);
                } else {
                    previous.node.remove();
                    node.classList.remove("is-entering", "is-entering-back");
                }
            }
            ui.dialog.setAttribute("aria-labelledby", def.headingId);
            if (def.leadId) ui.dialog.setAttribute("aria-describedby", def.leadId);
            else ui.dialog.removeAttribute("aria-describedby");
            if (ui.dialog.open) focusHeading(node);
            if (def.enter) def.enter(node);
        }

        function back() {
            if (busy) return;
            const name = history.pop();
            if (name) show(name, { back: true });
        }

        function focusHeading(node) {
            const heading = node.querySelector(".plx-onb-title");
            if (heading && heading.focus) heading.focus({ preventScroll: true });
        }

        function finish(result) {
            answer = result;
            ui.dialog.close();
        }

        // -- one screen, from its description ----------------------------

        function render(name, def) {
            const node = el("section", `plx-onb-screen${def.shake ? " is-error" : ""}`);
            node.dataset.screen = name;
            const hero = el("div", def.brand ? "plx-onb-brand-hero" : "plx-onb-hero");
            if (def.brand) {
                // The welcome screen: who this is, before anything is asked.
                if (ui.logo) {
                    const holder = el("span", "plx-onb-mark-wrap");
                    const mark = el("img", "plx-onb-mark");
                    mark.alt = "";
                    mark.src = ui.logo;
                    holder.appendChild(mark);
                    hero.appendChild(holder);
                }
            } else {
                const tile = el("span", `plx-onb-tile tone-${def.tone || "lite"}`);
                tile.setAttribute("aria-hidden", "true");
                tile.innerHTML = svg(def.art, true);
                hero.appendChild(tile);
            }
            const heading = el("h2", `plx-dialog-title plx-onb-title${def.brand ? " plx-onb-name" : ""}`);
            if (def.brand) heading.appendChild(el("span", "plx-onb-sr", "Welcome to "));
            heading.appendChild(doc.createTextNode(def.title));
            heading.id = uid("title");
            heading.tabIndex = -1;
            def.headingId = heading.id;
            hero.appendChild(heading);
            if (def.brand) hero.appendChild(ui.versioned(el("p", "plx-onb-name-version")));
            if (def.lead) {
                const lead = rich("p", "plx-onb-lead", def.lead);
                lead.id = uid("lead");
                def.leadId = lead.id;
                hero.appendChild(lead);
            }
            node.appendChild(hero);
            if (def.body && def.body.length) {
                const body = el("div", "plx-onb-body");
                def.body.forEach((part) => part && body.appendChild(part));
                node.appendChild(body);
            }
            // A quiet way out (a link) goes under the content, not into the
            // row of answers: three in a row do not fit beside Back.
            const links = (def.actions || []).filter((action) => action && action.kind === "link");
            const answers = (def.actions || []).filter((action) => action && action.kind !== "link");
            if (links.length) {
                const alt = el("div", "plx-onb-alt");
                links.forEach((action) => alt.appendChild(button(action)));
                node.appendChild(alt);
            }
            const hasActions = answers.length > 0;
            const foot = el("footer", `plx-onb-foot${hasActions ? "" : " is-back-only"}`);
            if (def.back && history.length) {
                foot.appendChild(button({ label: "Back", kind: "back", iconName: "arrowLeft", onClick: back, role: "back" }));
            } else {
                foot.appendChild(el("span", "plx-onb-foot-spacer"));
            }
            const actions = el("div", "plx-onb-actions");
            answers.forEach((action) => actions.appendChild(button(action)));
            foot.appendChild(actions);
            if (hasActions || (def.back && history.length)) node.appendChild(foot);
            if (def.footnote) node.appendChild(rich("p", "plx-onb-footnote", def.footnote));
            return node;
        }

        // -- what the buttons do -----------------------------------------

        //: The browser's folder dialog, from this click (the first thing it
        //: awaits), then what the folder is; `after(look)` takes it from there.
        async function locate(after, { startIn = null } = {}) {
            if (busy) return;
            let handle;
            try {
                handle = await store.pickFolder({ startIn });
            } catch (error) {
                failed(error, () => locate(after, { startIn }));
                return;
            }
            if (!handle) {
                ctx.retry = () => locate(after, { startIn });
                show("cancelled");
                return;
            }
            ctx.handle = handle;
            setBusy(true);
            try {
                ctx.look = await store.inspectFolder(handle);
            } catch (error) {
                setBusy(false);
                failed(error, () => locate(after, { startIn }));
                return;
            }
            setBusy(false);
            ctx.repick = () => locate(after, { startIn });
            after(ctx.look);
        }

        //: After "Locate my Plexora folder" (and Reconnect's "Choose another").
        function afterLocate(look) {
            if (look.kind === "workspace" || look.kind === "legacy-nested") show("found");
            else if (look.kind === "projects-inside") show("projects-inside");
            else show("unrecognised");
        }

        //: After "Choose folder" on Create your Plexora folder.
        function afterCreate(look) {
            if (look.kind === "workspace" || look.kind === "legacy-nested") show("existing");
            else if (look.kind === "projects-inside") show("projects-inside");
            else if (look.kind === "foreign") show("foreign");
            else adopt({ init: true });
        }

        //: The picked folder made the workspace, on the setup screen.
        async function adopt({ init }) {
            if (busy) return;
            ctx.result = null;
            show("setup");
            const tasks = current.def.tasks;
            setBusy(true);
            try {
                const done = await store.adoptWorkspace(ctx.handle, {
                    init, look: ctx.look, onProgress: (step) => tasks.reach(step),
                });
                ctx.result = { kind: "folder", label: done.label, desktopFinds: done.desktopFinds !== false,
                               moved: done.moved || [], look: done.look };
            } catch (error) {
                setBusy(false);
                tasks.fail();
                failed(error, () => adopt({ init }));
                return;
            }
            setBusy(false);
            tasks.reach("done");
            ready();
        }

        //: Browser storage, from this click: the request to keep it first
        //: (Firefox asks the user, and only from a click).
        async function useBrowserStorage() {
            if (busy) return;
            const storage = global.navigator && global.navigator.storage;
            const asking = storage && storage.persist ? storage.persist().catch(() => false) : Promise.resolve(false);
            ctx.path = ctx.path || "new";
            ctx.result = null;
            show("browser");
            const tasks = current.def.tasks;
            setBusy(true);
            let persisted = false;
            try {
                tasks.reach("prepare");
                const prepared = await store.prepareBrowser();
                if (prepared.probe === "quota") {
                    throw Object.assign(new Error("The browser has no room left for this site."), { code: "quota" });
                }
                tasks.reach("persist");
                persisted = Boolean(await asking)
                    || Boolean(storage && storage.persisted && await storage.persisted().catch(() => false));
                tasks.note("persist", persisted ? "The browser will keep your work"
                    : "The browser may clear it when space runs low");
                tasks.reach("record");
                // Set up again from a folder: back to the browser, and the
                // page starts again on it (`switchTo` reloads).
                if (store.current().kind === "folder") {
                    await store.switchTo("browser");
                    return;
                }
                await store.useBrowser();
            } catch (error) {
                setBusy(false);
                tasks.fail();
                failed(error, () => useBrowserStorage(), { where: "browser" });
                return;
            }
            setBusy(false);
            tasks.reach("done");
            ctx.result = { kind: "browser", persisted, storage: store.current().storage || null };
            ready();
        }

        //: The setup screen's last state: done, where, and on with the work.
        function ready() {
            const def = current.def;
            const node = current.node;
            const result = ctx.result;
            // The finish is laid out differently (centred, the place under
            // the check): its height follows, as a screen change's does.
            const from = ui.stage.offsetHeight;
            setStep(STEPS, true);
            const heading = node.querySelector(".plx-onb-title");
            heading.textContent = result.kind === "folder" ? "Your workspace is ready" : "Browser storage is ready";
            const lead = node.querySelector(".plx-onb-lead");
            if (lead) {
                lead.textContent = result.kind === "folder"
                    ? "Plexora saves your projects, annotations and figures in this folder."
                    : "Plexora saves your projects, annotations and figures in this browser.";
            }
            node.classList.add("is-finish");
            const tile = node.querySelector(".plx-onb-tile");
            tile.className = "plx-onb-tile tone-ok is-done";
            tile.innerHTML = svg(result.kind === "folder" ? "folderCheck" : "browserCheck", true);
            if (!reducedMotion()) {
                const burst = confetti();
                tile.appendChild(burst);
                global.setTimeout(() => burst.remove(), CONFETTI_MS + 700);
            }
            const body = node.querySelector(".plx-onb-body");
            body.classList.add("is-done");
            const notes = [];
            if (result.kind === "folder") {
                if (result.moved.length) notes.push(`${plural(result.moved.length, "project", "projects")} from this browser ${result.moved.length === 1 ? "was" : "were"} copied here.`);
                if (!result.desktopFinds) {
                    notes.push("The desktop app opens this folder once you add it there under Settings › Data › Locations.");
                }
            } else {
                if (result.storage === "memory") {
                    notes.push("This browser lets the site keep nothing: your work is lost when the tab closes.");
                } else if (!result.persisted) {
                    notes.push("The browser may clear it when space runs low or when browsing data is cleared. "
                               + "You can move it to a folder later in Settings › Data.");
                }
            }
            const done = el("div", "plx-onb-done");
            done.appendChild(place(result));
            // True while this browser keeps the choice (IndexedDB; services/
            // browserStore.js): cleared with the site's data, it is asked again.
            if (result.storage !== "memory") {
                done.appendChild(el("p", "plx-onb-once", "You won't be asked again in this browser."));
            }
            notes.forEach((text) => done.appendChild(el("p", "plx-onb-note", text)));
            body.replaceChildren(done);
            const foot = el("footer", "plx-onb-foot");
            foot.appendChild(el("span", "plx-onb-foot-spacer"));
            const actions = el("div", "plx-onb-actions");
            const go = button({ label: ctx.reason === "pick" ? "Continue to my image" : "Continue", kind: "primary",
                                trailing: "arrowRight", role: "continue", onClick: () => finish("ready") });
            actions.appendChild(go);
            foot.appendChild(actions);
            const old = node.querySelector(".plx-onb-foot");
            if (old) old.replaceWith(foot);
            else node.appendChild(foot);
            def.done = true;
            // Set up whichever way the dialog is closed now: the work it was
            // opened for goes on.
            answer = "ready";
            ui.live.textContent = heading.textContent;
            ui.dialog.dataset.phase = "done";
            if (!reducedMotion()) {
                const to = node.offsetHeight;
                ui.stage.style.height = `${from}px`;
                void ui.stage.offsetHeight;
                ui.stage.style.height = `${to}px`;
                ui.stage.classList.add("is-moving");
                global.setTimeout(() => {
                    ui.stage.style.height = "";
                    ui.stage.classList.remove("is-moving");
                }, 320);
            }
            if (go.focus) go.focus({ preventScroll: true });
        }

        //: From the Reconnect screen: the chosen folder asked for again.
        async function reconnectFolder() {
            if (busy) return;
            const asking = store.reconnect();
            setBusy(true);
            const control = current.node.querySelector('[data-role="reconnect"]');
            if (control) control.classList.add("is-working");
            let done;
            try {
                done = await asking;
            } catch (error) {
                done = { ok: false, reason: "failed", error: error && error.message };
            }
            setBusy(false);
            if (control) control.classList.remove("is-working");
            if (done.ok) {
                global.PlexoraToast?.show?.({ title: `Reconnected to “${done.label}”` });
                finish("ready");
                return;
            }
            if (done.reason === "denied") {
                ctx.retry = reconnectFolder;
                ctx.error = null;
                show("denied");
            } else if (done.reason === "missing") {
                show("reconnect");
            } else {
                failed(Object.assign(new Error(done.error || "The folder could not be opened."), { code: "partial" }),
                       reconnectFolder);
            }
        }

        function failed(error, retry, { where = "folder" } = {}) {
            ctx.retry = retry;
            ctx.error = error;
            ctx.where = where;
            const code = (error && error.code) || (error && error.name === "QuotaExceededError" ? "quota"
                : error && (error.name === "NotAllowedError" || error.name === "SecurityError") ? "denied" : "partial");
            const screens = { unsupported: "unsupported", denied: "denied", "not-writable": "not-writable",
                              quota: "quota", "projects-inside": "projects-inside",
                              "not-a-workspace": "unrecognised" };
            show(screens[code] || "partial");
        }

        // -- the screens --------------------------------------------------

        const folders = () => Boolean(store && store.canWriteFolders());
        const pickLabel = () => (ctx.look ? ctx.look.label || ctx.look.name : "that folder");
        const chooseAnother = () => (ctx.repick ? ctx.repick() : ctx.path === "new" ? show("create") : locate(afterLocate));
        const toBrowser = { label: "Use browser storage", kind: "secondary", onClick: useBrowserStorage,
                            role: "browser" };

        //: The progress lines of a setup: each reached in turn, the one before
        //: it ticked off.
        function taskList(rows) {
            const list = el("ol", "plx-onb-tasks");
            list.setAttribute("aria-label", "Progress");
            const items = new Map();
            rows.forEach(([key, text]) => {
                const item = el("li", "plx-onb-task is-pending");
                item.dataset.task = key;
                const mark = el("span", "plx-onb-task-mark");
                mark.setAttribute("aria-hidden", "true");
                item.appendChild(mark);
                item.appendChild(el("span", "plx-onb-task-text", text));
                list.appendChild(item);
                items.set(key, item);
            });
            const order = rows.map(([key]) => key);
            const aliases = { remember: "merge", python: "open" };
            const api = {
                node: list,
                reach(step) {
                    const key = aliases[step] || step;
                    const at = key === "done" ? order.length : order.indexOf(key);
                    if (at < 0) return;
                    order.forEach((name, index) => {
                        const item = items.get(name);
                        item.classList.remove("is-pending", "is-running", "is-done");
                        item.classList.add(index < at ? "is-done" : index === at ? "is-running" : "is-pending");
                    });
                    if (step === "open") api.note("open", rows.find(([name]) => name === "open")[1]);
                    if (step === "python") api.note("open", "Waiting for Plexora to finish starting");
                    if (key !== "done" && items.get(key)) ui.live.textContent = items.get(key).textContent;
                },
                note(key, text) {
                    const item = items.get(key);
                    if (item) item.querySelector(".plx-onb-task-text").textContent = text;
                },
                fail() {
                    items.forEach((item) => {
                        if (item.classList.contains("is-running")) {
                            item.classList.remove("is-running");
                            item.classList.add("is-failed");
                        }
                    });
                },
            };
            return api;
        }

        const SCREENS = {
            welcome: () => {
                const features = el("ul", "plx-onb-features");
                features.setAttribute("aria-label", "What Plexora does");
                FEATURES.forEach(([name, title, text]) => {
                    const item = el("li", "plx-onb-feature");
                    item.appendChild(icon(name, "plx-onb-feature-icon"));
                    const words = el("span", "plx-onb-feature-words");
                    words.appendChild(el("span", "plx-onb-feature-title", title));
                    words.appendChild(el("span", "plx-onb-feature-text", text));
                    item.appendChild(words);
                    item.style.setProperty("--i", String(features.children.length));
                    features.appendChild(item);
                });
                const start = el("div", "plx-onb-start");
                const head = el("div", "plx-onb-start-head");
                head.appendChild(el("p", "plx-onb-eyebrow", "How would you like to start?"));
                // Asked once: the answer is kept in this browser (services/
                // browserStore.js), and only clearing the site's data asks again.
                const once = el("p", "plx-onb-once-pill");
                once.appendChild(icon("clock", "plx-onb-once-icon"));
                once.appendChild(el("span", null, "One-time setup"));
                head.appendChild(once);
                start.appendChild(head);
                const grid = el("div", "plx-onb-cards is-start");
                grid.appendChild(card({
                    name: "returning", iconName: "history", title: "I've used Plexora before",
                    text: "Open your Plexora folder.",
                    onClick: () => { ctx.path = "returning"; show("returning"); },
                }));
                grid.appendChild(card({
                    name: "new", iconName: "sparkle", title: "I'm new to Plexora",
                    text: "Choose a place for your work.",
                    onClick: () => { ctx.path = "new"; show("home"); },
                }));
                Array.from(grid.children).forEach((child, index) => child.style.setProperty("--i", String(index)));
                start.appendChild(grid);
                start.appendChild(el("p", "plx-onb-start-note",
                                     "Plexora remembers your choice in this browser. You can change it in Settings › Data."));
                return {
                    step: 1, brand: true,
                    title: "Plexora",
                    lead: "View and analyse multiplexed tissue images in your browser.",
                    body: [features, start],
                };
            },

            returning: () => ({
                step: 2, art: "history", back: true,
                title: "Welcome back",
                lead: "Choose the folder Plexora saved your work in. It is usually **Documents › Plexora**.",
                body: [(() => {
                    const hint = el("div", "plx-onb-hint");
                    hint.appendChild(icon("folder", "plx-onb-hint-icon"));
                    hint.appendChild(rich("p", "plx-onb-hint-text",
                                          "Plexora recognises the folder by its **workspace.json**, or by the "
                                          + "**projects** folder inside it. Projects, figures and bookmarks come with it."));
                    return hint;
                })()],
                actions: [
                    { label: "Set up a new workspace", kind: "secondary", role: "new",
                      onClick: () => { ctx.path = "new"; show("home"); } },
                    { label: "Locate my Plexora folder", kind: "primary", iconName: "folderSearch", role: "locate",
                      onClick: () => (folders() ? locate(afterLocate) : show("unsupported")) },
                ],
            }),

            found: () => {
                const look = ctx.look;
                return {
                    step: 2, art: "folderCheck", tone: "ok", back: true,
                    title: "Found your workspace",
                    lead: look.kind === "legacy-nested"
                        ? `Your projects are in a Plexora folder inside **${look.name}**. Plexora uses it where it is; nothing is moved.`
                        : "Plexora recognised this folder. Its projects open here, and new work is saved beside them.",
                    body: [summary(look)],
                    actions: [
                        { label: "Choose another folder", kind: "secondary", role: "another",
                          onClick: () => locate(afterLocate) },
                        { label: "Continue", kind: "primary", trailing: "arrowRight", role: "continue",
                          onClick: () => adopt({ init: false }) },
                    ],
                };
            },

            unrecognised: () => {
                const look = ctx.look || { name: "That folder", kind: "foreign" };
                return {
                    step: 2, art: "folderQuestion", tone: "warn", back: true,
                    title: look.kind === "empty" ? `“${look.name}” is empty` : `“${look.name}” is not a Plexora folder`,
                    lead: "Plexora looked for **workspace.json**, or a **projects** folder with config.json in it, "
                        + "and found neither.",
                    body: [rich("p", "plx-onb-note", look.kind === "empty"
                        ? "Choose the folder your work is in, or set this one up as a new workspace."
                        : "Choose the folder your work is in, or set this one up: Plexora adds its own files "
                          + "beside what is there and changes nothing else.")],
                    actions: [
                        { label: "Start fresh in browser storage", kind: "link", role: "browser",
                          onClick: useBrowserStorage },
                        { label: "Set up this folder", kind: "secondary", role: "init",
                          onClick: () => adopt({ init: true }) },
                        { label: "Choose another folder", kind: "primary", role: "another",
                          onClick: () => locate(afterLocate) },
                    ],
                };
            },

            home: () => {
                const options = el("div", "plx-onb-cards is-options");
                if (folders()) {
                    options.appendChild(card({
                        name: "folder", iconName: "folder", title: "Use a folder on this computer", chip: "Recommended",
                        recommended: true,
                        text: "Saved as files you can see, back up, and open in the desktop app.",
                        onClick: () => { ctx.path = "new"; show("create"); },
                    }));
                } else {
                    const note = el("div", "plx-onb-hint is-warn");
                    note.appendChild(icon("browserOff", "plx-onb-hint-icon"));
                    note.appendChild(rich("p", "plx-onb-hint-text",
                                          "This browser cannot save into folders on your computer. "
                                          + "**Chrome** and **Edge** can, and so can the Plexora desktop app."));
                    options.appendChild(note);
                }
                options.appendChild(card({
                    name: "browser", iconName: "browser", title: "Use browser storage",
                    text: "Kept inside this browser.", quiet: folders(),
                    notes: ["May be cleared with browser data", "Not visible in Finder or File Explorer",
                            "Harder to open in the desktop app"],
                    onClick: useBrowserStorage,
                }));
                // The reason for the choice, read before it: what a folder
                // gives that the browser's storage does not (each line true of
                // a folder; the browser card says what the browser lacks).
                const benefits = el("ul", "plx-onb-benefits");
                benefits.setAttribute("aria-label", "A folder on this computer lets you");
                [["shieldCheck", "Keep your projects safe when browser data is cleared"],
                 ["pen", "Save analyses, annotations and figures as files"],
                 ["clock", "Continue later on this computer"],
                 ["monitor", "Open the same projects in Plexora desktop"],
                 ["folders", "Keep your workspace organised in one folder"]].forEach(([name, text]) => {
                    const item = el("li", "plx-onb-benefit");
                    item.appendChild(icon(name, "plx-onb-benefit-icon"));
                    item.appendChild(el("span", "plx-onb-benefit-text", text));
                    item.style.setProperty("--i", String(benefits.children.length));
                    benefits.appendChild(item);
                });
                // The two places rise after the reasons for them.
                Array.from(options.children).forEach((child, index) => {
                    child.style.setProperty("--i", String(benefits.children.length + index));
                });
                return {
                    step: 2, art: "home", back: true,
                    title: "Give your work a home",
                    lead: "A folder on this computer lets you:",
                    body: [benefits, options],
                };
            },

            create: () => {
                const steps = el("ol", "plx-onb-steps");
                ["In the window that opens, go to **Documents**.",
                 "Create a folder named **Plexora** with New Folder.",
                 "Select that new **Plexora** folder.",
                 "Allow access when the browser asks."].forEach((text) => {
                    // One box for the words: the line is a grid, its number
                    // the first column.
                    const line = el("li", "plx-onb-stepline");
                    line.appendChild(rich("span", "plx-onb-stepline-text", text));
                    steps.appendChild(line);
                });
                const layout = el("div", "plx-onb-create");
                layout.appendChild(steps);
                layout.appendChild(folderTree());
                return {
                    step: 3, art: "folderPlus", back: true,
                    title: "Create your Plexora folder",
                    lead: "Plexora saves your work in **Documents › Plexora**, where the desktop app looks too.",
                    body: [layout,
                           rich("p", "plx-onb-note", "If the window does not open in Documents, find Documents in "
                                + "its sidebar: a browser never lets a site use Documents itself, only a folder in it. "
                                + "Already have a Plexora folder? Choose it: what is in it is kept.")],
                    actions: [
                        { label: "Choose folder", kind: "primary", iconName: "folder", role: "choose",
                          onClick: () => locate(afterCreate, { startIn: "documents" }) },
                    ],
                };
            },

            existing: () => ({
                step: 3, art: "folderCheck", tone: "ok", back: true,
                title: "This folder already has Plexora data",
                lead: `**${pickLabel()}** holds ${plural(ctx.look.projects.length, "project", "projects")}. `
                    + "Plexora opens them and adds your work beside them. Nothing is overwritten.",
                body: [summary(ctx.look)],
                actions: [
                    { label: "Choose another folder", kind: "secondary", role: "another",
                      onClick: () => locate(afterCreate, { startIn: "documents" }) },
                    { label: "Use this folder", kind: "primary", role: "use", onClick: () => adopt({ init: false }) },
                ],
            }),

            foreign: () => ({
                step: 3, art: "folderFiles", back: true,
                title: `“${pickLabel()}” already has other files`,
                lead: "Plexora adds **workspace.json** and its **projects** and **pyramids** folders beside them, "
                    + "and changes nothing else.",
                body: [ctx.look && ctx.look.name.toLowerCase() !== "plexora"
                    ? rich("p", "plx-onb-note", "The desktop app looks in **Documents › Plexora**, so a folder "
                           + "of that name is the easier choice.")
                    : null],
                actions: [
                    { label: "Choose another folder", kind: "secondary", role: "another",
                      onClick: () => locate(afterCreate, { startIn: "documents" }) },
                    { label: "Use this folder", kind: "primary", role: "use", onClick: () => adopt({ init: true }) },
                ],
            }),

            setup: () => {
                const look = ctx.look || {};
                const existing = look.kind === "workspace" || look.kind === "legacy-nested";
                const tasks = taskList([
                    ["probe", `Checking Plexora can save in “${pickLabel()}”`],
                    ["folders", existing ? "Reading the workspace" : "Adding workspace.json, projects and pyramids"],
                    ["merge", "Bringing over what this browser holds"],
                    ["open", "Opening the workspace"],
                ]);
                return {
                    step: 3, art: "folderDown", transient: true,
                    title: "Setting up your workspace",
                    lead: `In **${pickLabel()}**.`,
                    body: [tasks.node],
                    tasks,
                };
            },

            browser: () => {
                const tasks = taskList([
                    ["prepare", "Preparing storage in this browser"],
                    ["persist", "Asking the browser to keep your work"],
                    ["record", "Saving this choice"],
                ]);
                return {
                    step: 3, art: "browser", transient: true,
                    title: "Setting up browser storage",
                    lead: "In this browser, on this computer.",
                    body: [tasks.node],
                    tasks,
                };
            },

            reconnect: () => {
                const now = store.current();
                const label = now.notOpened || now.label || "your Plexora folder";
                const missing = Boolean(now.missing);
                return {
                    step: null, art: "plug", tone: "warn",
                    title: "We couldn't reconnect to your Plexora folder",
                    lead: missing
                        ? `**${label}** could not be found. It may have been moved, renamed or deleted.`
                        : `Your work is saved in **${label}**. The browser needs your permission again before `
                          + "Plexora can open it.",
                    // Not a setup: the folder was chosen before. This is only
                    // the browser asking again, which it does until told
                    // "Allow on every visit" (Chrome 122+; an installed site never asks).
                    body: [missing ? null : rich("p", "plx-onb-note", "This is needed only when the browser forgets its "
                                                  + "permission. Choose **Allow on every visit** when it asks, and the "
                                                  + "folder opens without this step next time.")],
                    actions: [
                        { label: "Continue with browser storage", kind: "link", role: "browser",
                          onClick: () => { store.continueInBrowser(); finish("ready"); } },
                        { label: "Choose another folder", kind: missing ? "primary" : "secondary", role: "another",
                          onClick: () => { ctx.path = "reconnect"; locate(afterLocate); } },
                        missing ? null : { label: "Reconnect folder", kind: "primary", iconName: "folder",
                                           role: "reconnect", onClick: reconnectFolder },
                    ],
                };
            },

            cancelled: () => ({
                art: "folderX", tone: "neutral", back: true, transient: true, shake: true,
                title: "No folder was chosen",
                lead: "The folder window closed before a folder was selected."
                    + (ctx.reason === "pick" ? " Your image is still waiting." : ""),
                actions: [
                    toBrowser,
                    { label: "Choose folder", kind: "primary", iconName: "folder", role: "retry",
                      onClick: () => ctx.retry && ctx.retry() },
                ],
            }),

            denied: () => ({
                art: "shieldX", tone: "danger", back: true, transient: true, shake: true,
                title: "Plexora wasn't allowed to use that folder",
                lead: "The browser asked whether this site may edit files there, and the answer was no. "
                    + "Plexora needs that permission to save your work in a folder.",
                actions: [
                    toBrowser,
                    { label: "Try again", kind: "primary", role: "retry", onClick: () => ctx.retry && ctx.retry() },
                ],
            }),

            unsupported: () => {
                const get = el("a", "plx-onb-link", null);
                get.href = DOWNLOAD;
                get.target = "_blank";
                get.rel = "noopener";
                get.appendChild(el("span", null, "Get the desktop app"));
                get.appendChild(icon("external", "plx-onb-button-icon is-trailing"));
                return {
                    art: "browserOff", tone: "warn", back: true, transient: true, shake: true,
                    title: "This browser can't save to a folder",
                    lead: "Firefox and Safari do not let websites save files into folders on your computer. "
                        + "Chrome and Edge do, and so does the Plexora desktop app.",
                    body: [rich("p", "plx-onb-note", "Your work can be kept in this browser's storage instead. "
                                + "It stays in this browser only."), get],
                    actions: [{ label: "Use browser storage", kind: "primary", onClick: useBrowserStorage,
                                role: "browser" }],
                };
            },

            "not-writable": () => ({
                art: "lock", tone: "danger", back: true, transient: true, shake: true,
                title: `Plexora can't save in “${pickLabel()}”`,
                lead: "The folder is read-only, or the browser did not allow changes to it. Choose a folder you "
                    + "can edit, such as one in Documents.",
                actions: [
                    toBrowser,
                    { label: "Choose another folder", kind: "primary", role: "another", onClick: chooseAnother },
                ],
            }),

            quota: () => {
                const inBrowser = ctx.where === "browser";
                return {
                    art: "disk", tone: "warn", back: true, transient: true, shake: true,
                    title: "There is no room left to save",
                    lead: inBrowser
                        ? "The browser refused to store more for this site. Free up space on this computer, or "
                          + "clear older site data, then try again."
                        : `The disk holding **${pickLabel()}** is full. Free up space, or choose a folder on another disk.`,
                    actions: inBrowser
                        ? [folders() ? { label: "Use a folder on this computer", kind: "secondary", role: "folder",
                                         onClick: () => { ctx.path = "new"; show("create"); } } : null,
                           { label: "Try again", kind: "primary", role: "retry", onClick: () => ctx.retry && ctx.retry() }]
                        : [{ label: "Choose another folder", kind: "secondary", role: "another", onClick: chooseAnother },
                           { label: "Try again", kind: "primary", role: "retry", onClick: () => ctx.retry && ctx.retry() }],
                };
            },

            partial: () => {
                const detail = ctx.error && ctx.error.message ? el("p", "plx-onb-detail", ctx.error.message) : null;
                return {
                    art: "warning", tone: "warn", back: true, transient: true, shake: true,
                    title: "The workspace was only partly set up",
                    lead: `Plexora stopped part-way through setting up **${pickLabel()}**. Nothing was deleted, and `
                        + "what is already there is kept: trying again finishes it.",
                    body: [detail],
                    actions: [
                        { label: "Use browser storage", kind: "link", role: "browser", onClick: useBrowserStorage },
                        { label: "Choose another folder", kind: "secondary", role: "another", onClick: chooseAnother },
                        { label: "Try again", kind: "primary", role: "retry", onClick: () => ctx.retry && ctx.retry() },
                    ],
                };
            },

            "projects-inside": () => ({
                art: "folderUp", tone: "warn", back: true, transient: true, shake: true,
                title: `“${pickLabel()}” is the projects folder inside a workspace`,
                lead: "Plexora needs the folder that holds **projects**, usually **Documents › Plexora**. A website "
                    + "can only use the folder it is given, never the one above it.",
                actions: [{ label: "Choose another folder", kind: "primary", role: "another", onClick: chooseAnother }],
            }),
        };

        // -- open ----------------------------------------------------------

        return new Promise((resolve) => {
            ui.dialog.addEventListener("cancel", (event) => {
                // Escape: a cancel, as the X is -- not while work is under way.
                if (busy) event.preventDefault();
            });
            ui.dialog.addEventListener("close", () => {
                ui.dialog.remove();
                resolve(answer);
            });
            doc.body.appendChild(ui.dialog);
            show(start);
            if (typeof ui.dialog.showModal !== "function") {
                ui.dialog.remove();
                resolve("cancelled");
                return;
            }
            ui.dialog.showModal();
            focusHeading(current.node);
        });
    }

    // -- the API ----------------------------------------------------------------

    let pending = null;

    /**
     * "ready" once a place for the user's work is chosen and open (asked
     * here when it is not), "cancelled" when the dialog is closed. One dialog
     * at a time: a second call while it is up gets the same answer.
     */
    function ensure({ reason = "save" } = {}) {
        const store = storeOf();
        if (!store || typeof store.needsOnboarding !== "function") return Promise.resolve("ready");
        if (pending) return pending;
        const start = store.needsReconnect() ? "reconnect" : store.needsOnboarding() ? "welcome" : null;
        if (!start) return Promise.resolve("ready");
        pending = run({ reason, start }).finally(() => { pending = null; });
        return pending;
    }

    //: The dialog whatever was chosen before: Settings' "Set up again".
    function open({ reason = "settings", start = "welcome" } = {}) {
        if (!storeOf()) return Promise.resolve("cancelled");
        if (pending) return pending;
        pending = run({ reason, start }).finally(() => { pending = null; });
        return pending;
    }

    global.PlexoraOnboarding = Object.freeze({ ensure, open });
})(window);
