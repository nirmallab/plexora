/**
 * Import Sample: one dialog, three states, no wizard.
 *
 * The whole of importing data into Plexora. Somebody presses one button,
 * points at files or a folder, sees what Plexora found, and presses Import.
 * There are no format tabs, no role-labelled fields and no second page.
 *
 *   pick       One path field with the machine chip at its front, and Browse
 *              (Add once a path is typed). Browse takes a file or a folder in
 *              one go -- see browsePicker.js -- so nothing asks which first.
 *              Format chips under it, and the way to the rest. Scoped, it is
 *              a card's compact add row and nothing else.
 *   proposal   One card per sample, one row per detected layer, each saying
 *              what it becomes. A question appears UNDER the row it concerns,
 *              with its default already chosen, and never blocks Import.
 *              A card's "+ Add segmentation mask / data / layer" opens ONE
 *              compact row under it -- machine chip, path, Browse -- where a
 *              browsed file is read at once and a typed path waits for Load.
 *   importing  The same rows become a progress rail, and the sample opens as
 *              soon as its record exists -- whatever is still building keeps
 *              building behind the viewer, which already knows how to wait.
 *
 * A line under the title carries the whole of the dialog's voice: what to do,
 * then what pressing Import would create, then what is happening. Nothing
 * inside the body repeats it. The `?` beside the close button opens
 * views/importHelp.js, which is where formats, folder layouts and rules are
 * documented -- so this dialog never has to explain itself in place.
 *
 * The three states are one `<dialog>` that changes in place rather than three
 * screens: a modal that grew a Back button would be a wizard, and the entire
 * point is that the default path is one gesture and one press.
 *
 * Nothing here decides what a file IS. `POST /import/inspect` answers that,
 * and this draws the answer -- so a new vendor format becomes a row in this
 * list with no change to this file. Detection is also the only thing that
 * puts a question on screen: a question here is a failure of detection, shown
 * where it applies, not a step in a flow.
 *
 * Scoped mode (`open({sample})`) is the same dialog for "+ Add Layer": no name
 * and no dataset, everything proposed as a layer of a sample that already
 * exists, and the viewer adopts the result without a reload unless a MASK was
 * added -- which shifts channel indices and is the one case a reload is for.
 * `open({sample, intent})` is the sidebar's "Add Seg Mask / Add Data": the same
 * scoped dialog, saying what it was opened for. Detection still decides what
 * the file IS -- the intent only settles a file the pixels leave open -- and
 * every row's role badge is a menu wherever the file has another reading, so
 * an image detected as a mask is corrected in place rather than removed.
 *
 * Served as a classic script from base.html, after datasetPicker.js: the
 * dataset row is that picker, and the library page and the viewer both open
 * this.
 */
window.PlexoraImportSample = (function () {
    "use strict";

    //: How often the importing state asks what has landed. The same interval
    //: the mask's own wait uses, for the same reason: fast enough that a short
    //: build does not look stalled, slow enough that a long one is not a poll
    //: storm.
    const POLL_MS = 1500;
    //: While the POST itself runs. Its steps move every few seconds.
    const REGISTER_POLL_MS = 700;

    //: How long the dialog stays up after a successful import before it opens
    //: the sample. Long enough to read "registered" against each row, short
    //: enough that it never feels like a step.
    const OPEN_AFTER_MS = 1200;

    //: A glyph per layer kind. The kind is a rendering strategy, so this is
    //: honest about what the row will look like rather than about what the
    //: data is -- the modality is in the words beside it.
    const KIND_ICON = {
        image: "fa-layer-group",
        labels: "fa-shapes",
        points: "fa-braille",
        shapes: "fa-draw-polygon",
        table: "fa-table",
        warning: "fa-triangle-exclamation",
        info: "fa-circle-info",
    };

    //: What each row BECOMES, as the badge ranged right on it. `role` is where
    //: the thing is stored -- the image spec, the segmentation, the feature
    //: table, a layer -- which is the distinction a row's name and its detail
    //: line do not carry and the one somebody scanning for "did it find my
    //: mask?" is looking for. Never coloured: a role is a fact, not a status.
    const ROLE_BADGE = {
        image: "Image",
        mask: "Mask",
        table: "Table",
        layer: "Layer",
        //: Recorded on the sample and not read -- a Xenium expression matrix.
        //: Saying so on the row is the difference between a feature somebody
        //: is waiting for and a bug they report.
        note: "Recorded",
    };

    //: What a single pick that expands into a whole sample is called, keyed
    //: by the `format` on the bundle the server sends back.
    const BUNDLE_WORD = {
        xenium: "a Xenium run",
        spatialdata: "a SpatialData store",
        visium: "a Visium run",
        visium_hd: "a Visium HD run",
    };

    //: The same four in the summary line's own register, where they are read
    //: as a list rather than as a label.
    const ROLE_WORD = {
        image: "image",
        mask: "mask",
        table: "cell table",
        layer: "layer",
        note: "recorded data",
    };

    //: The steps of pyramidizing a flat image, keyed as the server keys them
    //: (data_model.IMAGE_PYRAMID_STAGES). Drawn as a small rail inside the
    //: image's own row, so the wait names what it is doing -- a copy of a
    //: large stack is minutes, and one moving row says nothing about where.
    const PYRAMID_STEPS = [
        ["opening", "Open the source image"],
        ["inspecting", "Check its dimensions"],
        ["preparing", "Prepare to read"],
        ["building", "Build pyramid levels"],
        ["writing", "Finalize the file"],
        ["registering", "Register and load"],
    ];

    let dialog = null;
    let state = null;

    //: The machine the last pick that read was on, so the next dialog opens
    //: there: a slide on hpc has its mask on hpc. Kept for the page and in
    //: localStorage across reloads, and offered only while that machine is
    //: still connected -- opening on one nobody can browse is a wrong default
    //: the user then has to notice and undo.
    const WHERE_KEY = "plexora.import.where";
    let lastWhere = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    /** Whether this dialog is scoped to one existing sample. */
    function scoped() {
        return Boolean(state && state.sample);
    }

    // -- the dialog itself --------------------------------------------------

    function build() {
        const node = document.createElement("dialog");
        node.className = "plx-dialog plx-import";
        node.innerHTML = `
            <div class="plx-import-head">
                <div class="plx-import-head-text">
                    <h2 class="plx-dialog-title" data-role="title">Import sample</h2>
                    <p class="plx-import-subtitle" data-role="subtitle"></p>
                </div>
                <div class="plx-import-head-actions">
                    <button class="plx-picker-close plx-import-help" type="button"
                            data-role="help" title="How importing works"
                            aria-label="How importing works" aria-haspopup="dialog">
                        <span class="fas fa-circle-question" aria-hidden="true"></span>
                    </button>
                    <button class="plx-picker-close" type="button" data-role="close"
                            title="Close" aria-label="Close">
                        <span class="fas fa-xmark" aria-hidden="true"></span>
                    </button>
                </div>
            </div>
            <div class="plx-import-body" data-role="body"></div>
            <p class="plx-import-status" data-role="status" role="status" aria-live="polite"></p>
            <div class="plx-import-foot">
                <button class="plx-button" type="button" data-role="add" hidden>
                    <span class="fas fa-plus" aria-hidden="true"></span>
                    <span data-role="add-text">Add more samples</span>
                </button>
                <span class="plx-import-blocked" data-role="reason"></span>
                <button class="plx-button plx-button-primary" type="button" data-role="go" disabled>
                    Import sample
                </button>
            </div>`;
        node.querySelector('[data-role="close"]').addEventListener("click", close);
        // The `?`. Opens on Formats when something on screen was not
        // recognised, because that is the question actually being asked --
        // "what DOES it read?" -- and on Overview otherwise.
        node.querySelector('[data-role="help"]').addEventListener("click", (event) => {
            window.PlexoraImportHelp?.open({
                tab: (state?.proposal?.unrecognised || []).length
                    ? "formats" : "overview",
                returnTo: event.currentTarget,
            });
        });
        // The FOOTER's add, which is "another sample" and carries no intent:
        // a file that belongs to a sample already on screen is added on that
        // sample's own card, where it can say which sample it joins. This is
        // the way back to an empty picker.
        node.querySelector('[data-role="add"]').addEventListener("click", () => {
            state.adding = null;
            render("pick");
            // `autofocus` only fires when the dialog OPENS. Coming back here
            // from a proposal, the focus has to be placed by hand or it stays
            // on a button that is no longer on screen.
            state?.pickFocus?.focus();
        });
        // A click anywhere else in the dialog puts the machine menu away, as
        // every menu in the navbar does. The same for a card's Browse menu.
        node.addEventListener("click", (event) => {
            const machine = state && state.machine;
            if (machine && menuOpen() && !machine.menu.contains(event.target)
                && !machine.button.contains(event.target)) {
                closeMenu(false);
            }
            const browse = state && state.browseMenu;
            if (browse && !browse.menu.hidden && !browse.menu.contains(event.target)
                && !(browse.anchor && browse.anchor.contains(event.target))) {
                closeBrowseMenu(false);
            }
        });
        // Wrapped rather than passed straight in: `submit` takes the name of a
        // sample it is replacing, and a listener would hand it a MouseEvent.
        node.querySelector('[data-role="go"]').addEventListener("click", () => submit());
        // Escape closes; the browser fires `cancel` for it. Prevented while an
        // import is in flight -- the work is server-side and would carry on
        // with nothing on screen saying so.
        node.addEventListener("cancel", (event) => {
            if (state && state.phase === "importing") event.preventDefault();
            else close();
        });
        portal("attach", node);
        return node;
    }

    /**
     * Put the dialog where a fullscreen viewer can still see it.
     *
     * `PopoverPortal` is a classic script's top-level `const`, which is a
     * shared binding and NOT a property of `window` -- reaching for it through
     * `window` silently does nothing, and a <dialog> that was never appended
     * throws on `showModal`. Guarded by `typeof` because this dialog also opens
     * from the library page, which loads the portal but need not.
     *
     * The fallback is <body>, which is what every other dialog here uses and
     * is right everywhere except inside a fullscreened subtree -- which is
     * exactly the case "+ Add Layer" can be opened from.
     */
    function portal(verb, node) {
        if (typeof PopoverPortal !== "undefined") {
            PopoverPortal[verb](node);
            return;
        }
        if (verb === "attach") document.body.appendChild(node);
        else node.remove();
    }

    function part(role) {
        return dialog ? dialog.querySelector(`[data-role="${role}"]`) : null;
    }

    /**
     * The dialog's one live line. `quiet` keeps it in the accessibility tree
     * but off the screen, for what a card's row is already showing in place:
     * a screen reader still hears "Reading mask.tif…", and nobody sees the
     * same news twice. Transparent rather than hidden, so the footer under it
     * does not move.
     */
    function setStatus(message, isError, quiet) {
        const status = part("status");
        if (!status) return;
        status.textContent = message || "";
        status.classList.toggle("is-error", Boolean(message) && Boolean(isError));
        status.classList.toggle("is-quiet", Boolean(message) && Boolean(quiet));
    }

    function close() {
        if (!dialog) return;
        const after = state && state.onClose;
        // Whatever was imported is bound to a project by now and the server
        // keeps it; what was only looked at stops being served.
        // Not mid-import (Escape is refused then, but a caller can still
        // close): a resource about to be bound must not be pulled first.
        if (state && state.phase !== "importing") release(state.picks);
        if (state && state.machine && state.machine.unwatch) state.machine.unwatch();
        stopReadingOrb();
        state = null;
        try {
            dialog.close();
        } catch (error) { /* already closed */ }
        portal("detach", dialog);
        dialog = null;
        if (after) after();
    }

    /**
     * Open the dialog.
     *
     * @param sample - an existing sample's name, for "+ Add Layer". Scoped
     *   mode: no name, no dataset, everything added to that sample.
     * @param modality - filter the invitation text to one kind of data, for
     *   the requirements modal's "Add layer..." row.
     * @param dataset - `{id, name}` to preselect, for importing from inside a
     *   dataset folder.
     * @param onClose - called when the dialog goes away, whatever happened.
     *   The library page re-lists from it.
     * @param paths - files or folders to start with: what was dropped on the
     *   desktop app's window, or opened from Explorer/Finder.
     */
    function open(options) {
        options = options || {};
        if (dialog) close();
        state = {
            phase: "pick",
            picks: [],
            //: The picks the proposal on screen was computed FROM, so a row's
            //: `pick` index always resolves against the array it indexes.
            //: `state.picks` can be one ahead of it -- a removal edits it and
            //: re-renders the old proposal while the next inspection is in
            //: flight -- and indexing THAT is how a second fast ✕ took out the
            //: file below the one it was on.
            picked: [],
            proposal: null,
            answers: {},
            sample: options.sample || null,
            modality: options.modality || null,
            dataset: options.dataset || null,
            //: Per sample, keyed by `SampleProposal.key`: `{name, dataset}`,
            //: present only where the user has said something. Keyed by what
            //: the sample IS rather than by its position, because an added
            //: mask can merge two cards into one and renumber the rest.
            meta: {},
            //: Whether a dataset has been chosen in this dialog yet. The first
            //: choice is everybody's (see `chooseDataset`).
            datasetSaid: false,
            //: `SampleProposal.key` -> outline colour index. See `tintFor`.
            tints: {},
            //: Question id -> true once its ✓ was pressed: drawn as one line
            //: saying what was chosen rather than as the control. The answer
            //: itself is in `answers`; this is only how much room it takes.
            settled: {},
            //: `{key, role}` while a card's inline "choose a file" row is
            //: open. One at a time: two open rows on one screen is two places
            //: a path could go and no way to tell which. `fresh` until it has
            //: been drawn once (it eases in once, not on every render) and
            //: `draft` holding what was typed but not yet sent, which a
            //: re-render for some other card must not throw away.
            adding: null,
            //: The card's pick while the server reads it: `{path, origin,
            //: intent, address, isNew}`. The row stays open, holding the path
            //: and a spinner, until the answer lands -- then it closes, or
            //: says why not. One at a time; a second Load is ignored.
            //: `origin` is "browse" or "typed": a path the native picker
            //: returned is read at once and never passes through the typed
            //: state, so it never shows a Load button.
            addFlight: null,
            //: The pick whose rows to light once, as they first appear.
            arrived: null,
            //: Where the keyboard goes after the next render: `{key, role}`
            //: names a card's slot, `focusTarget` the element itself.
            focusAfter: null,
            focusTarget: null,
            //: A card's Browse menu (Choose file / Choose folder), built
            //: once and hung under whichever row is open.
            browseMenu: null,
            node: null,
            location: null,
            //: The machine chip, its menu and the location service's status
            //: line -- built once and moved into whichever field is on screen.
            machine: null,
            //: What a scoped dialog was opened to add -- "mask", "table" or
            //: null -- for the sidebar's "Add Seg Mask / Add Data". Says what
            //: the pick is FOR; detection still says what it is.
            intent: options.sample && ["mask", "table", "cells"].includes(options.intent)
                ? options.intent : null,
            //: Called with `/import/layers`' answer once layers were added in
            //: place (no reload), so the control that opened this can repaint.
            onAdded: options.onAdded || null,
            onClose: options.onClose || null,
        };
        dialog = build();
        part("title").textContent = !scoped() ? "Import sample"
            : state.intent === "mask" ? `Add a segmentation mask to ${state.sample}`
            : state.intent === "table" ? `Add data to ${state.sample}`
            : state.intent === "cells" ? `Add a mask or cell data to ${state.sample}`
            : `Add a layer to ${state.sample}`;
        part("go").textContent = scoped() ? "Add layers" : "Import sample";
        // "Add more samples" is a promise the scoped dialog cannot keep: it
        // adds layers to one sample that already exists.
        part("add-text").textContent = scoped()
            ? "Add more files" : "Add more samples";
        mountLocation();
        render("pick");
        dialog.showModal();
        if (Array.isArray(options.paths) && options.paths.length) {
            if (options.proposal) showRead(options.paths, options.proposal);
            else addPaths(options.paths);
        }
        return dialog;
    }

    /**
     * Paths the caller already inspected, drawn as they were read rather than
     * read again. The home page reads a pick to decide whether one press can
     * open it; when it cannot, the dialog it hands over to would otherwise
     * spend the same seconds learning the same thing. Only ever an inspection
     * of exactly these paths with no answers -- what a fresh dialog's own
     * first read would have been.
     */
    function showRead(paths, proposal) {
        state.picks = paths.slice();
        state.picked = paths.slice();
        state.proposal = proposal;
        render("proposal");
    }

    /** Several picks at once, inspected once. */
    function addPaths(paths) {
        paths.forEach((path) => {
            if (path && !state.picks.includes(path)) state.picks.push(path);
        });
        state.adding = null;
        inspect();
    }

    /**
     * Paths dropped on the desktop app's window: into the dialog if it is
     * open, a new dialog otherwise. The shell delivers a drop as PATHS, so
     * nothing is uploaded and nothing is too big to drop.
     */
    function dropPaths(paths) {
        if (!Array.isArray(paths) || !paths.length) return;
        if (dialog && state && (state.phase === "pick" || state.phase === "proposal")) {
            addPaths(paths);
            return;
        }
        open({paths});
    }

    /**
     * Which machine, as the home page asks it: a chip at the front of the
     * path field ("Local", or the server's name) whose menu lists the actual
     * machines -- `views/quickViewLanding.js`, to the item.
     *
     * The service is attached `bare`: it keeps the state (what a path on
     * another machine submits as) and this draws the switch. The chip, its
     * menu and the service's status line are built ONCE, because the body is
     * rebuilt with innerHTML on every render and the service holds the status
     * element it was given -- so each render moves them into the field on
     * screen rather than making new ones.
     */
    function mountLocation() {
        if (!window.PlexoraDataLocation || !window.PlexoraDataLocation.available()) return;
        const box = document.createElement("input");
        box.type = "hidden";
        dialog.appendChild(box);
        const machine = buildMachine();
        try {
            state.location = window.PlexoraDataLocation.attach(box, {
                kind: "image",
                bare: true,
                statusMount: machine.status,
                onChange: () => paintMachine(),
            });
        } catch (error) {
            console.error("importSample: no location switch.", error);
        }
        if (state.location) state.machine = machine;
        paintMachine();
        restoreWhere();
    }

    /** Note the machine a pick that just read was on, for the next dialog. */
    function rememberWhere() {
        if (!state || !state.location) return;
        const place = state.location.isLocal() ? null : state.location.place();
        lastWhere = place ? {id: place.id, kind: place.kind, label: place.label,
                             node: place.node || null} : null;
        try {
            if (lastWhere) localStorage.setItem(WHERE_KEY, JSON.stringify(lastWhere));
            else localStorage.removeItem(WHERE_KEY);
        } catch (error) { /* storage refused: this page remembers it anyway */ }
    }

    /**
     * Open on the machine the last successful pick came from, if it is still
     * up. The list of machines may not have arrived yet, so this waits for it
     * once -- and gives up if the user has chosen a machine by then, or the
     * dialog has gone.
     */
    function restoreWhere() {
        let wanted = lastWhere;
        if (!wanted) {
            try {
                wanted = JSON.parse(localStorage.getItem(WHERE_KEY) || "null");
            } catch (error) { wanted = null; }
        }
        if (!wanted || !wanted.id || !state.location || !Remotes()) return;
        const owner = state;
        const apply = (snapshot) => {
            if (state !== owner || !state.location.isLocal()) return;
            const place = (snapshot.places || []).find((entry) => entry.id === wanted.id);
            if (!place || !(place.kind === "server" || place.node || place.files)) return;
            state.location.setWhere("remote", {id: place.id, kind: place.kind,
                                               label: place.label, node: place.node || null,
                                               files: place.files || null});
            paintMachine();
        };
        const now = Remotes().snapshot();
        if (now && now.loaded) {
            apply(now);
            return;
        }
        let stop = null;
        let done = false;
        stop = Remotes().subscribe((snapshot) => {
            if (done) return;
            done = true;
            apply(snapshot);
            if (stop) stop();
        });
        // Answered from inside subscribe(), before `stop` existed.
        if (done) stop();
    }

    const Remotes = () => window.PlexoraRemotes;

    function buildMachine() {
        const button = el("button", "plx-import-machine");
        button.type = "button";
        button.setAttribute("aria-haspopup", "menu");
        button.setAttribute("aria-expanded", "false");
        // Every icon in the markup from the start: Font Awesome swaps each
        // <span> for an <svg> once, so a class changed on the span afterwards
        // changes nothing. Which one shows is CSS over `.is-remote`.
        button.innerHTML = `
            <span class="plx-import-machine-local" aria-hidden="true"><span class="fas fa-laptop"></span></span>
            <span class="plx-import-machine-dot" aria-hidden="true"></span>
            <span class="plx-import-machine-label">Local</span>
            <span class="plx-import-machine-chevron" aria-hidden="true"><span class="fas fa-chevron-down"></span></span>`;
        const menu = el("div", "plx-import-menu");
        menu.setAttribute("role", "menu");
        menu.setAttribute("aria-label", "Where the files are");
        menu.hidden = true;
        // In the dialog rather than in the field: the body scrolls, and a menu
        // inside it would be clipped by it. Fixed-positioned under the chip.
        dialog.appendChild(menu);
        const status = el("div", "plx-import-machine-status");

        button.addEventListener("click", () => {
            if (menuOpen()) closeMenu(true);
            else openMenu();
        });
        button.addEventListener("keydown", (event) => {
            if (event.key === "ArrowDown" && !menuOpen()) {
                event.preventDefault();
                openMenu();
            }
        });
        menu.addEventListener("keydown", (event) => {
            const items = menuItems();
            const at = items.indexOf(document.activeElement);
            if (event.key === "Escape") {
                // The dialog's own Escape would close the whole dialog.
                event.preventDefault();
                event.stopPropagation();
                closeMenu(true);
            } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                const step = event.key === "ArrowDown" ? 1 : -1;
                items[(at + step + items.length) % items.length]?.focus();
            } else if (event.key === "Home" || event.key === "End") {
                event.preventDefault();
                items[event.key === "Home" ? 0 : items.length - 1]?.focus();
            } else if (event.key === "Tab") {
                closeMenu(false);
            }
        });
        return {button, menu, status, unwatch: null, key: ""};
    }

    function paintMachine() {
        const machine = state && state.machine;
        if (!machine) return;
        const remote = !state.location.isLocal();
        const place = remote ? state.location.place() : null;
        const name = remote ? ((place && (place.label || place.id)) || "Remote") : "Local";
        machine.button.classList.toggle("is-remote", remote);
        machine.button.querySelector(".plx-import-machine-label").textContent = name;
        machine.button.setAttribute("aria-label",
            `Files are on ${remote ? name : "this computer"}. Change machine`);
        machine.button.title = remote ? `Files on ${name}` : "Files on this computer";
    }

    function menuOpen() {
        return Boolean(state && state.machine && !state.machine.menu.hidden);
    }

    function menuItems() {
        return Array.from(state.machine.menu.querySelectorAll(".plx-import-menu-item:not(:disabled)"));
    }

    function openMenu() {
        const machine = state.machine;
        if (!machine || menuOpen() || machine.button.disabled) return;
        closeBrowseMenu(false);
        machine.key = "";
        drawMenu((Remotes() && Remotes().snapshot().places) || []);
        const rect = machine.button.getBoundingClientRect();
        machine.menu.style.left = `${Math.round(rect.left - 2)}px`;
        machine.menu.style.top = `${Math.round(rect.bottom + 10)}px`;
        machine.menu.hidden = false;
        machine.button.setAttribute("aria-expanded", "true");
        // `active`: the list is on screen, so a machine that comes up while it
        // is open shows here.
        if (Remotes()) {
            machine.unwatch = Remotes().subscribe(
                (snapshot) => drawMenu(snapshot.places || []), {active: true});
        }
        menuItems()[0]?.focus();
    }

    function closeMenu(refocus) {
        const machine = state && state.machine;
        if (!machine || machine.menu.hidden) return;
        machine.menu.hidden = true;
        machine.button.setAttribute("aria-expanded", "false");
        if (machine.unwatch) machine.unwatch();
        machine.unwatch = null;
        if (refocus) machine.button.focus();
    }

    function menuItem({icon, dot, name, sub, checked, onPick, href, role, disabled, add}) {
        const item = document.createElement(href ? "a" : "button");
        item.className = "plx-import-menu-item" + (add ? " is-add" : "");
        item.setAttribute("role", role || "menuitem");
        if (role === "menuitemradio") item.setAttribute("aria-checked", checked ? "true" : "false");
        if (href) {
            item.href = href;
        } else {
            item.type = "button";
            item.disabled = Boolean(disabled);
            item.addEventListener("click", onPick);
        }
        const lead = el("span", dot || "plx-import-menu-icon");
        lead.setAttribute("aria-hidden", "true");
        if (!dot) lead.appendChild(el("span", `fas fa-${icon}`));
        item.append(lead, el("span", "plx-import-menu-name", name));
        if (sub) item.append(el("span", "plx-import-menu-sub", sub));
        if (checked) {
            const tick = el("span", "plx-import-menu-check");
            tick.setAttribute("aria-hidden", "true");
            tick.appendChild(el("span", "fas fa-check"));
            item.append(tick);
        }
        return item;
    }

    function drawMenu(places) {
        const machine = state && state.machine;
        if (!machine) return;
        const remote = !state.location.isLocal();
        const current = remote && state.location.place() ? state.location.place().id : null;
        const key = JSON.stringify([current, places.map((place) =>
            [place.id, place.node, place.state, place.phase])]);
        // A poll that changes nothing must not rebuild the menu under the
        // keyboard focus.
        if (key === machine.key) return;
        machine.key = key;
        const focused = machine.menu.contains(document.activeElement)
            ? document.activeElement.dataset.place : null;
        const sep = () => {
            const line = el("div", "plx-import-menu-sep");
            line.setAttribute("role", "separator");
            return line;
        };
        const rows = [menuItem({
            icon: "laptop", name: "Local", sub: "This computer", role: "menuitemradio",
            checked: !remote, onPick: () => choose("local", null),
        }), sep(), el("div", "plx-import-menu-label", "Remote")];
        if (!places.length) rows.push(el("div", "plx-import-menu-empty", "No servers yet."));
        places.forEach((place) => {
            const up = place.kind === "server" || Boolean(place.node)
                || Boolean(place.files);
            const waiting = !up && Remotes() && Remotes().isOpening(place.state);
            const row = menuItem({
                dot: "plx-import-menu-dot" + (up ? "" : (waiting ? " is-waiting" : " is-off")),
                name: place.label,
                sub: up ? "Connected" : (waiting ? (place.phase || "Connecting…") : "Not connected"),
                role: "menuitemradio",
                checked: place.id === current,
                disabled: waiting,
                onPick: () => (up
                    ? choose("remote", {id: place.id, kind: place.kind,
                                        label: place.label, node: place.node || null,
                                        files: place.files || null})
                    : connect(place)),
            });
            row.dataset.place = place.id;
            rows.push(row);
        });
        rows.push(sep());
        rows.push(menuItem({icon: "plus", name: "Add a server…", add: true,
                            onPick: () => connect(null)}));
        rows.push(menuItem({icon: "gear", name: "Manage servers", sub: "Settings",
                            href: plexoraUrl("settings") + "#remotes"}));
        machine.menu.replaceChildren(...rows);
        if (focused) {
            machine.menu.querySelector(`[data-place="${CSS.escape(focused)}"]`)?.focus();
        }
    }

    /** Move the service to a machine, and go back to the box it governs. */
    function choose(where, place) {
        closeMenu(false);
        if (state.location) state.location.setWhere(where, place);
        paintMachine();
        state.pickInput?.focus();
    }

    /**
     * Connect a saved server that is not up yet, or add a new one, through the
     * connection modal, and use it the moment it is. Cancelling leaves the
     * machine as it was.
     */
    async function connect(place) {
        closeMenu(false);
        if (!window.PlexoraConnectionModal) return;
        const outcome = await window.PlexoraConnectionModal.open(place
            ? {name: place.id, kind: "node",
               intent: place.files_only
                   ? "Opening an SFTP connection to this machine, so images on "
                     + "it can be streamed in. Nothing is started there."
                   : "Opening a data node on this machine, so files on it "
                     + "can be imported."}
            : {kind: "node",
               intent: "Add the machine the files are on. Plexora will handle "
                       + "the SSH connection."});
        if (!state) return;
        if (outcome && outcome.connected) {
            choose("remote", {id: outcome.name, kind: "remote",
                              label: outcome.label || (place && place.label) || outcome.name,
                              node: outcome.node || null, files: outcome.files || null});
        } else {
            state.machine?.button.focus();
        }
    }

    function browseNode() {
        return state.location ? state.location.browseNode() : null;
    }

    /** Whether a browse machine is a files-only server's `sftp://` base. */
    function isFilesBase(node) {
        return Boolean(node) && /^sftp:\/\//i.test(String(node));
    }

    // -- state 1: pick ------------------------------------------------------

    function renderPick() {
        const body = part("body");
        body.innerHTML = "";
        if (scoped()) {
            renderScopedPick(body);
            return;
        }
        // Whatever a card was being added to, this is not it: the pick state's
        // field adds loose files.
        state.adding = null;
        // No heading over the control: the subtitle in the header already said
        // what to do, and saying it twice on one screen is half the clutter
        // this state used to carry.
        const pick = buildPickControl({
            examples: "sample",
            placeholder: "Paste a path, or drop a table here…",
            onTyped: (typed) => addPick(typed),
        });
        body.appendChild(pick);
        // Where `showModal()` puts the keyboard. Without it the browser
        // focuses the first focusable descendant, which is the × -- so the
        // dialog opened with a cyan ring around Close and nothing else.
        // `autofocus` is honoured on open and inert afterwards, which is why
        // "Add more samples" focuses it by hand.
        state.pickFocus.autofocus = true;

        // Coming back here from a proposal to add another sample. Without a
        // way back, the only route to the cards is to pick a file -- so
        // pressing Add and changing your mind meant losing sight of what you
        // already had.
        if (state.picks.length && state.proposal) {
            const back = el("button", "plx-import-back",
                            "← Back to what was found");
            back.type = "button";
            back.addEventListener("click", () => render("proposal"));
            body.appendChild(back);
        }

        part("add").hidden = true;
        part("go").disabled = state.picks.length === 0;
        if (state.picks.length) {
            // Coming back here from a proposal to add more. The button still
            // imports what is already there.
            part("go").disabled = false;
        }
        // A disabled primary with nothing beside it reads as a broken button.
        setReason(state.picks.length ? "" : "Choose a file or folder to continue");
    }

    /**
     * Scoped, the pick state is the card's add row and nothing else:
     *
     *     [Local ▾ | Paste a file or folder path…      ✕] [ Browse ]
     *
     * The sample already exists, so there is one thing to ask for -- a path --
     * and it is asked the way that sample's own card would ask it: browsed is
     * read at once, typed waits for Load, a path that cannot be read stays in
     * the box with the reason under it. What it was opened for (`intent`) is
     * the slot: "+ Add segmentation mask" settles a file whose pixels could be
     * either, and nothing else.
     */
    function renderScopedPick(body) {
        if (!state.adding) {
            // "cells" -- a mask or a table, either -- says nothing about the
            // file, so it is added as anything else is and read for what it is.
            state.adding = {key: state.sample,
                            role: ["mask", "table"].includes(state.intent)
                                ? state.intent : "layer",
                            fresh: true, draft: ""};
        }
        body.appendChild(renderInlinePick(null));
        if (state.picks.length && state.proposal) {
            const back = el("button", "plx-import-back", "← Back to what was found");
            back.type = "button";
            back.addEventListener("click", () => {
                state.adding = null;
                render("proposal");
            });
            body.appendChild(back);
        }
        part("add").hidden = true;
        part("go").disabled = state.picks.length === 0;
        setReason(state.picks.length ? "" : "Choose a file or folder to continue");
    }

    //: Two examples a side under the pick state's field, the way the home card
    //: shows two under its own -- formats a SAMPLE arrives in rather than one
    //: image, so a slide and a table on the file side and the two vendor runs
    //: somebody most often points at on the folder side. A card's inline
    //: picker shows its slot's own pair from browsePicker.js's KIND_EXAMPLES.
    const PICK_CHIPS = {file: [".ome.tiff", ".h5ad"], directory: ["Xenium", "Visium"]};
    //: The same on a licence without spatial transcriptomics, which does not
    //: detect either run at import (import_proposal._licensed) -- so the folder
    //: side names the run layouts that licence does read. Not greyed, absent.
    const PICK_CHIPS_WITHOUT_TRANSCRIPTOMICS = {
        file: [".ome.tiff", ".h5ad"], directory: ["SpatialData", ".zarr"]};

    function chipsFor(examples) {
        if (examples === "sample") {
            return window.PlexoraPaid?.allows?.("transcriptomics")
                ? PICK_CHIPS : PICK_CHIPS_WITHOUT_TRANSCRIPTOMICS;
        }
        const table = (typeof KIND_EXAMPLES !== "undefined" && KIND_EXAMPLES[examples]) || {};
        const pair = (text) => String(text || "").split(" · ").filter(Boolean).slice(0, 2);
        return {file: pair(table.file), directory: pair(table.directory)};
    }

    /**
     * The home page's card, as this dialog's pick control: one field with the
     * machine chip at its front, and one button that is Browse while the box
     * is empty and Add once a path is typed -- so the typed way in and the
     * browsed way in never look like two routes. No File | Folder pair: Browse
     * takes either (browsePicker.js), because which one a format is -- an
     * OME-Zarr store is a folder -- is not something to ask first. Under them,
     * the formats as chips, and the way to the rest.
     *
     * Used by the pick state and by a card's inline "Add ..." row, which is
     * why it takes its examples and placeholder rather than knowing them. The
     * whole control takes a drop -- small tables, which a browser can hand
     * over as bytes.
     */
    function buildPickControl({examples, placeholder, onTyped}) {
        const pick = el("div", "plx-import-pick");
        pick.dataset.kind = "any";
        pick.dataset.face = "browse";

        const row = el("div", "plx-import-pick-row");
        const field = el("div", "plx-import-field");
        if (state.machine) {
            field.appendChild(state.machine.button);
            state.machine.button.disabled = false;
        }
        const input = el("input", "plx-import-input");
        input.type = "text";
        input.placeholder = placeholder;
        input.spellcheck = false;
        input.autocomplete = "off";
        input.setAttribute("aria-label", "Path");
        field.appendChild(input);
        row.appendChild(field);

        const go = el("button", "plx-import-browse");
        go.type = "button";
        go.innerHTML = `
            <span class="plx-import-browse-face is-browse">
                <span class="fas fa-folder-open" aria-hidden="true"></span><span>Browse</span>
            </span>
            <span class="plx-import-browse-face is-add">
                <span>Add</span><span class="plx-import-browse-arrow" aria-hidden="true"><span class="fas fa-arrow-right"></span></span>
            </span>`;
        row.appendChild(go);
        pick.appendChild(row);

        const take = () => {
            const typed = input.value.trim();
            if (!typed) return false;
            input.value = "";
            pick.dataset.face = "browse";
            onTyped(typed);
            return true;
        };
        input.addEventListener("input", () => {
            pick.dataset.face = input.value.trim() ? "add" : "browse";
            if (input.getAttribute("aria-invalid")) {
                input.removeAttribute("aria-invalid");
                pick.classList.remove("is-refused");
            }
        });
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") take();
        });
        go.addEventListener("click", () => {
            if (!take()) pickWith("any");
        });

        // The service's own line -- "Sharing…", "Not connected to hpc yet" --
        // under the row it is about.
        if (state.machine) pick.appendChild(state.machine.status);

        const foot = el("div", "plx-import-formats");
        const list = el("ul", "plx-import-chips");
        list.setAttribute("aria-label", "Example formats");
        const shown = chipsFor(examples);
        ["file", "directory"].forEach((side) => {
            (shown[side] || []).forEach((label, index) => {
                const chip = el("li", "plx-import-chip"
                                + (side === "directory" && index === 0 ? " is-lead" : ""), label);
                chip.dataset.kind = side;
                list.appendChild(chip);
            });
        });
        list.appendChild(el("li", "plx-import-more", "and more"));
        const help = el("button", "plx-import-supported");
        help.type = "button";
        help.setAttribute("aria-haspopup", "dialog");
        help.innerHTML = `<span class="fas fa-circle-question" aria-hidden="true"></span><span>What’s supported</span>`;
        help.addEventListener("click", () => {
            window.PlexoraImportHelp?.open({tab: "formats", returnTo: help});
        });
        foot.append(list, help);
        pick.appendChild(foot);

        // Files dropped on the control. Small tables only, and the placeholder
        // says so: a browser cannot give a server the PATH of a 40 GB image,
        // only its bytes, and uploading a slide through a form is not an
        // import.
        pick.addEventListener("dragover", (event) => {
            event.preventDefault();
            pick.classList.add("is-over");
        });
        pick.addEventListener("dragleave", (event) => {
            if (!pick.contains(event.relatedTarget)) pick.classList.remove("is-over");
        });
        pick.addEventListener("drop", onDrop);

        //: Held so it can be disabled while something is in flight -- a
        //: second press while a file dialog is opening opens a second one.
        state.pickControls = [go];
        state.pickFocus = go;
        state.pickInput = input;
        return pick;
    }

    /**
     * Why the primary button is not pressable, beside the primary button.
     *
     * Only ever about the PICKS. An unanswered question is not a reason --
     * every question has a default or is deferred, and none of them blocks an
     * import -- so putting one here would tell the user to do something the
     * dialog does not actually require of them.
     */
    function setReason(text) {
        const reason = part("reason");
        if (reason) reason.textContent = text || "";
    }

    async function onDrop(event) {
        event.preventDefault();
        const drop = event.currentTarget;
        drop.classList.remove("is-over");
        // In the desktop app the same drop also arrives natively, as paths
        // (desktopBridge.js -> dropPaths). Uploading the bytes as well would
        // import it twice, and lose where it lives.
        if (window.PlexoraDesktop) return;
        const files = Array.from(event.dataTransfer?.files || []);
        if (!files.length) return;
        const big = files.find((file) => file.size > 64 * 1024 * 1024);
        if (big) {
            window.PlexoraConfirm?.tell({
                title: "Too big to drop",
                body: "A browser can only hand Plexora a dropped file's BYTES, "
                    + "not where it lives — which is fine for a table and wrong "
                    + "for a slide. For images and run folders, use Select.",
            });
            return;
        }
        // Read before the first await: the card's inline row is torn down by
        // the re-render that follows, and `state.adding` with it.
        const intent = state.adding;
        setStatus("Uploading…");
        for (const file of files) {
            const form = new FormData();
            form.append("file", file);
            try {
                const response = await fetch(plexoraUrl("upload_data_file"),
                                             {method: "POST", body: form});
                const result = await response.json();
                if (!response.ok || !result.path) throw new Error(result.error || "");
                if (!state.picks.includes(result.path)) state.picks.push(result.path);
                if (intent) sayAbout(result.path, intent);
            } catch (error) {
                setStatus(`Could not take ${file.name}.`, true);
                return;
            }
        }
        state.adding = null;
        setStatus(null);
        inspect();
    }

    function setBusy(busy) {
        (state?.pickControls || []).forEach((control) => { control.disabled = busy; });
    }

    function pickWith(mode) {
        setStatus("Opening file browser…");
        setBusy(true);
        const settle = setTimeout(() => setStatus(null), 1500);
        //: Read at the moment the browser OPENS, because the pick that comes
        //: back belongs to the card the user pressed -- and a card's inline
        //: row is torn down by the re-render that follows.
        const intent = state.adding;
        browseForPath({
            mode,
            filter: "any",
            node: browseNode(),
            start: startFor(intent),
            onPicked: (path) => {
                clearTimeout(settle);
                setStatus(null);
                // A card's row reads what was chosen at once: choosing it
                // in the system's own browser WAS the confirmation, and a
                // Load button after it would ask for it twice.
                if (intent) submitSlot(path, "browse", intent);
                else addPick(path, intent);
            },
            onUnavailable: () => {
                clearTimeout(settle);
                const why = "The file browser could not be opened — paste the "
                          + "full path instead.";
                if (intent && state && sameIntent(state.adding, intent)) {
                    setStatus(null);
                    state.addError = {typed: "", reason: why};
                    render(pickPhase());
                } else {
                    setStatus(why, true);
                }
            },
        }).finally(() => {
            clearTimeout(settle);
            setBusy(false);
        });
    }

    /**
     * Where a card's browser opens: beside the files already in that sample,
     * because a mask or a table almost always sits next to its slide. Only on
     * the machine being browsed -- a folder on hpc means nothing to this
     * computer's dialog -- and the folder rather than the file, or a file
     * dialog would offer the slide itself as the answer. The native dialog on
     * the server's own desktop ignores it and remembers its own last folder;
     * the desktop app's dialog and a cluster's listing both open there.
     */
    function startFor(intent) {
        if (!intent || !state) return "";
        const sample = (state.proposal?.samples || []).find((entry) => entry.key === intent.key);
        const node = browseNode();
        const filesBase = isFilesBase(node) ? `${node.replace(/\/+$/, "")}/` : "";
        for (const layer of sample?.layers || []) {
            let path = String(pickOf(layer) || "");
            // On a files-only server the picks ARE addresses on it, and the
            // listing takes one as it is and opens in its folder.
            if (filesBase && path.startsWith(filesBase)) return path;
            if (!path || window.PlexoraLocators?.isRemoteLocator(path)) continue;
            if (path.startsWith("node://")) {
                const prefix = `node://${node}/`;
                if (!node || !path.startsWith(prefix)) continue;
                path = path.slice(prefix.length);
            } else if (node) {
                continue;
            }
            const trimmed = path.replace(/[\\/]+$/, "");
            const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
            if (cut > 0) return trimmed.slice(0, cut);
            if (cut === 0) return trimmed.slice(0, 1);
        }
        return "";
    }

    function sameIntent(left, right) {
        return Boolean(left && right && left.key === right.key && left.role === right.role);
    }

    /**
     * Send what a card's row holds -- browsed or typed -- and keep the row
     * open and busy until the server has said what it is.
     *
     * The validation and the attaching are the dialog's own: `addPick` files
     * the pick against the card (`sample-for`, `added-as`) and `inspect()`
     * asks `/import/inspect`, so a mask added here is exactly a mask added
     * any other way. What this adds is only the waiting in place: the row
     * closes when the file lands on the card (`landFlight`), or keeps the
     * path and says why when it does not (`bouncePendingPick`, `failFlight`).
     */
    function submitSlot(path, origin, intent) {
        const typed = String(path || "").trim();
        if (!typed || !state || !intent || state.addFlight) return;
        closeBrowseMenu(false);
        state.adding = intent;
        intent.draft = "";
        state.addError = null;
        state.addFlight = {path: typed, origin, intent};
        addPick(typed, intent);
    }

    /**
     * Take a path, and say what it was added AS if a card's action added it.
     *
     * @param intent - `{key, role}` from the card whose slot was pressed:
     *   which sample the file joins, and which modality the user said it is.
     *   Both travel in `answers` rather than in a second array beside `paths`
     *   -- an array would have to stay aligned with a list the server filters
     *   and the user removes from, and `answers` survives re-inspection,
     *   reaches the Python API unchanged, and has nothing to line up with.
     *   Null for a loose pick, which is the whole of the old behaviour.
     */
    function addPick(path, intent) {
        const node = browseNode();
        // A path on another machine is addressed rather than read: the node is
        // the only process that can open it. A web address is left exactly as
        // typed either way -- it already names the machine it is on, and
        // wrapping an `s3://` URL in `node://` would ask a data node to open a
        // path it has never heard of instead of asking this server to read
        // the bucket directly.
        const remote = window.PlexoraLocators && window.PlexoraLocators.isRemoteLocator(path);
        // A files-only server is not a node: a path on it becomes the `sftp://`
        // address this server streams it from, the way a pasted URL is read.
        let address = path;
        if (!remote && isFilesBase(node)) {
            const files = (state.location && state.location.place()
                           && state.location.place().files) || {};
            address = window.PlexoraLocators.sftpUrl(node, path, files.home);
        } else if (node && !remote) {
            address = `node://${node}/${path}`;
        }
        // Deduplicated: the same file picked twice is one pick, and two rows
        // over one file is two ✕ buttons that each look broken.
        const isNew = !state.picks.includes(address);
        if (isNew) state.picks.push(address);
        if (intent) {
            sayAbout(address, intent);
            // Remembered so a path that turns out to be unreadable can be
            // taken back out and the box reopened with the reason under it.
            // Without this the box closed before the answer arrived, and a
            // failed pick that was the card's only file took the whole card
            // with it -- the reason landed in a loose row nobody connected
            // to what they had just typed.
            state.pending = isNew ? {
                address, path, intent,
                proposal: state.proposal, picked: state.picked,
            } : null;
        }
        state.addError = null;
        state.justPicked = true;
        // A card's row stays open, busy, while this is read (`submitSlot`);
        // every other way in closes whatever row was open.
        if (state.addFlight) Object.assign(state.addFlight, {address, isNew});
        else state.adding = null;
        inspect();
    }

    /**
     * File what a card said about a pick: which sample it joins, and as what.
     *
     * Under the WHOLE pick, never its filename. An mcmicro run names every
     * sample's mask `cellRing.ome.tif`, so keyed by name, adding sample 2's
     * mask overwrote the answer that tied sample 1's to its card -- and both
     * masks landed on one sample. The server reads these back the same way
     * (`import_proposal._said`).
     */
    function sayAbout(pick, intent) {
        state.answers[`sample-for:${pick}`] = intent.key;
        state.answers[`added-as:${pick}`] = intent.role;
    }

    /**
     * Forget every answer about one pick: the card's own statements, and each
     * question the server asked about a row that came out of it. The row's
     * `needs` are those questions' ids, so nothing here has to rebuild how
     * the server spells them.
     */
    function forgetAbout(pick) {
        delete state.answers[`sample-for:${pick}`];
        delete state.answers[`added-as:${pick}`];
        const at = (state.picked || []).indexOf(pick);
        (state.proposal?.samples || []).forEach((sample) => {
            (sample.layers || []).forEach((layer) => {
                if (at < 0 || layer.pick !== at) return;
                (layer.needs || []).forEach((id) => { delete state.answers[id]; });
                // A confirmation is not in `needs` -- it never holds anything
                // up -- but a file removed and picked again is asked again.
                // So is anything its ✓ put away.
                questionsFor(sample, `layer:${layer.id}`).forEach((question) => {
                    if (question.kind === "confirm") delete state.answers[question.id];
                    delete state.settled[question.id];
                });
            });
        });
    }

    /**
     * Tell the server which picks the dialog let go of, so a data node stops
     * serving what reading them made it share. Fire-and-forget: it runs on
     * the way out, and the server keeps what it could not release for a
     * later try. Only `node://` picks can have shared anything.
     */
    function release(picks) {
        const remote = (picks || []).filter(
            (pick) => String(pick).startsWith("node://"));
        if (!remote.length) return;
        fetch(plexoraUrl("import/release"), {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({paths: remote}),
            keepalive: true,
        }).catch(() => {});
    }

    // -- state 2: proposal --------------------------------------------------

    //: Guards against an out-of-order answer. Every inspection is a round trip
    //: and the user can answer a second question while the first is in flight;
    //: without this the older answer's proposal can land last and undo it.
    let inspectToken = 0;

    /**
     * A response's JSON, or an `{error}` that says what came back instead.
     *
     * An answer that is not JSON -- a server error page, a proxy's -- used to
     * reach the user as the parser's own complaint, `Unexpected token '<',
     * "<!doctype "... is not valid JSON`, which names nothing they can act on.
     */
    async function readJson(response) {
        const text = await response.text();
        try {
            return JSON.parse(text);
        } catch (error) {
            return {error: `Plexora's server failed (HTTP ${response.status}) `
                + "and did not say why. The terminal Plexora is running in "
                + "has the details."};
        }
    }

    async function inspect() {
        const token = ++inspectToken;
        render("proposal");
        // A card's row shows its own reading, in place, and so does the
        // subtitle before the first proposal; this line then says it only to
        // a screen reader.
        const flight = state.addFlight;
        if (flight) setStatus(`Reading ${basename(flight.path)}…`, false, true);
        else setStatus("Looking at what is there…", false, !state.proposal);
        //: The exact array this proposal's `pick` indices will refer to.
        const sent = state.picks.slice();
        try {
            const response = await fetch(plexoraUrl("import/inspect"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    paths: sent,
                    answers: state.answers,
                    sample: state.sample || undefined,
                }),
            });
            const proposal = await readJson(response);
            if (token !== inspectToken || !state) return;
            if (!response.ok) throw new Error(proposal.error);
            if (bouncePendingPick(proposal, sent)) return;
            state.proposal = proposal;
            state.picked = sent;
            setStatus(null);
            if (state.justPicked) {
                state.justPicked = false;
                rememberWhere();
            }
            const landed = landFlight();
            render("proposal");
            if (landed) setStatus(landed);
        } catch (error) {
            if (token !== inspectToken || !state) return;
            if (failFlight(error.message)) return;
            setStatus(error.message || "Plexora could not read those files.", true);
        }
    }

    /**
     * A card's pick was read and is on the card: close its row, light the
     * rows it became, and put the keyboard back on the card. Returns the
     * line to say so, or null when no card's pick was in flight.
     *
     * The row closes only if it is still the one the pick came from --
     * somebody who opened another card's row meanwhile keeps it.
     */
    function landFlight() {
        const flight = state.addFlight;
        if (!flight) return null;
        state.addFlight = null;
        state.arrived = flight.address || null;
        if (sameIntent(state.adding, flight.intent)) {
            state.adding = null;
            state.focusAfter = {key: flight.intent.key, role: flight.intent.role};
        }
        const sample = (state.proposal?.samples || []).find(
            (entry) => entry.key === flight.intent.key);
        const name = sample && (state.meta[sample.key]?.name || sample.name);
        return name ? `Added ${basename(flight.path)} to ${name}.`
                    : `Added ${basename(flight.path)}.`;
    }

    /**
     * The server could not be asked at all while a card's pick was in flight
     * -- a dropped connection, a crash. The pick goes back out (it was never
     * read, and Import pressed now must not carry it) and the row reopens
     * holding the path, with the reason under it. True when it did.
     */
    function failFlight(message) {
        const flight = state.addFlight;
        if (!flight) return false;
        state.addFlight = null;
        state.pending = null;
        if (flight.isNew && flight.address) {
            state.picks = state.picks.filter((pick) => pick !== flight.address);
            delete state.answers[`sample-for:${flight.address}`];
            delete state.answers[`added-as:${flight.address}`];
            release([flight.address]);
        }
        state.adding = flight.intent;
        state.addError = {typed: flight.path,
                          reason: message || "Plexora could not read that path."};
        setStatus(null);
        render(pickPhase());
        return true;
    }

    /**
     * Where an open add row is drawn: on its card, or -- scoped, with nothing
     * read yet -- as the whole pick state, there being no card to hold it.
     * "proposal" with no proposal is the reading skeleton, which with nothing
     * in flight would wait for ever.
     */
    function pickPhase() {
        return scoped() && !state.proposal?.samples?.length ? "pick" : "proposal";
    }

    /**
     * A path pasted into a card that could not be read goes back where it came
     * from: out of the picks, the card as it was, and the box open again
     * holding what was typed, with the reason directly under it. True when it
     * did, so the caller does not draw the proposal that dropped the card.
     */
    function bouncePendingPick(proposal, sent) {
        const pending = state.pending;
        state.pending = null;
        if (!pending) return false;
        const index = sent.indexOf(pending.address);
        const refused = (proposal.unrecognised || []).find(
            (entry) => entry.pick === index);
        if (index < 0 || !refused) return false;
        state.picks = state.picks.filter((pick) => pick !== pending.address);
        delete state.answers[`sample-for:${pending.address}`];
        delete state.answers[`added-as:${pending.address}`];
        state.addFlight = null;
        state.justPicked = false;
        state.adding = pending.intent;
        state.addError = {typed: pending.path, reason: refused.reason};
        if (pending.proposal) {
            state.proposal = pending.proposal;
            state.picked = pending.picked;
        } else if (scoped() && !state.picks.length) {
            // The first pick of a scoped dialog: nothing else on screen to
            // keep, so the row it came from is the whole of the answer.
            state.proposal = null;
            state.picked = [];
            setStatus(null);
            render("pick");
            return true;
        } else {
            state.proposal = proposal;
            state.picked = sent;
        }
        setStatus(null);
        render("proposal");
        return true;
    }

    /**
     * The body while the first read is out: one sample card in outline -- its
     * head and three rows, at the sizes the real ones take -- so what arrives
     * replaces it in place rather than a graphic giving way to a layout.
     * Shapes only, never words: a placeholder row that said "Mask" would be a
     * claim about files nobody has read yet.
     */
    function readingSkeleton() {
        const card = el("div", "plx-import-sample plx-import-skeleton");
        card.setAttribute("aria-hidden", "true");
        const head = el("div", "plx-import-card-head");
        head.appendChild(el("span", "plx-import-bone is-head"));
        card.appendChild(head);
        for (let index = 0; index < 3; index += 1) {
            const row = el("div", "plx-import-row");
            row.appendChild(el("span", "plx-import-bone is-icon"));
            const text = el("div", "plx-import-row-text");
            text.appendChild(el("span", "plx-import-bone is-name"));
            text.appendChild(el("span", "plx-import-bone is-detail"));
            row.appendChild(text);
            row.appendChild(el("span", "plx-import-bone is-role"));
            card.appendChild(row);
        }
        return card;
    }

    /**
     * The orb beside "Reading …": the agent panel's thinking orb at its chip
     * size, in its "searching" state and the dialog's accent -- a working
     * indicator the size of the line it belongs to. A spinner on a page with
     * no orb driver.
     */
    function readingOrb() {
        const orb = window.PlexoraOrb;
        if (!orb || typeof orb.mount !== "function") {
            const spinner = el("span", "plx-import-spinner");
            spinner.setAttribute("aria-hidden", "true");
            return spinner;
        }
        const canvas = el("canvas", "plx-import-reading-orb");
        try {
            state.readingOrb = orb.mount(canvas, {
                state: "searching", size: 20, tint: accentColour(), dark: true,
            });
        } catch (error) {
            canvas.setAttribute("data-orb", "static");
        }
        return canvas;
    }

    function stopReadingOrb() {
        if (!state || !state.readingOrb) return;
        state.readingOrb.destroy();
        state.readingOrb = null;
    }

    function accentColour() {
        try {
            return window.getComputedStyle(dialog || document.documentElement)
                .getPropertyValue("--accent-channel").trim() || undefined;
        } catch (error) {
            return undefined;
        }
    }

    function renderProposal() {
        const body = part("body");
        body.innerHTML = "";
        const proposal = state.proposal;
        part("add").hidden = false;

        if (!proposal) {
            // The subtitle says what is being read, with the orb working
            // beside it; the body holds the shape of the card about to arrive,
            // so the answer lands where the eye already is. Three lines saying
            // "Reading…" once each was the same news three times.
            body.appendChild(readingSkeleton());
            part("go").disabled = true;
            setReason("");
            return;
        }

        const samples = proposal.samples || [];
        samples.forEach((sample, index) => {
            body.appendChild(renderSample(sample, index, samples.length));
        });

        (proposal.unrecognised || []).forEach((entry) => {
            const row = el("div", "plx-import-row is-warning");
            row.appendChild(iconFor("warning"));
            const text = el("div", "plx-import-row-text");
            text.appendChild(el("span", "plx-import-row-name", basename(entry.path)));
            text.appendChild(el("span", "plx-import-row-detail", entry.reason));
            row.appendChild(text);
            row.appendChild(removeButton(pickOf(entry)));
            body.appendChild(row);
        });

        // A warning is about the pick as a whole rather than about one file,
        // so it has no row to sit under and no ✕ to leave it out with.
        (proposal.warnings || []).forEach((warning) => {
            const row = el("div", "plx-import-row is-warning");
            row.appendChild(iconFor("warning"));
            const text = el("div", "plx-import-row-text");
            text.appendChild(el("span", "plx-import-row-detail", warning));
            row.appendChild(text);
            body.appendChild(row);
        });

        // Enabled as soon as ANY sample has a layer. An unreadable file among
        // five must not stop the other four from being imported.
        const importable = (proposal.samples || []).some((s) => (s.layers || []).length);
        part("go").textContent = goLabel(samples);
        setReason(importable ? "" : "Nothing to import");
        part("go").disabled = !importable;
        // Held while a card's pick is being read: Import pressed now would
        // import the sample without the file just chosen for it, and "Add
        // more samples" would be pulled back here when the answer landed.
        if (state.addFlight) {
            part("go").disabled = true;
            part("add").disabled = true;
        } else {
            part("add").disabled = false;
        }
        // Lit once, on the render that first shows them.
        state.arrived = null;
    }

    /**
     * What the primary button says it will do, counted.
     *
     * "Import sample" over a screen showing two of them is the one place this
     * dialog could mislead somebody into a second press.
     */
    function goLabel(samples) {
        const layers = samples.reduce(
            (total, sample) => total + (sample.layers || []).length, 0);
        if (scoped()) return layers === 1 ? "Add layer" : `Add ${layers} layers`;
        return samples.length > 1
            ? `Import ${samples.length} samples` : "Import sample";
    }

    /**
     * The one line under the title: what pressing Import would create.
     *
     * Presentation only -- every number here is already on the proposal. A
     * bundle counts as ONE source, because a Xenium run is one thing the user
     * picked and "1 sample from 6 files" describes a folder they chose once.
     */
    function summarize(proposal) {
        const samples = proposal.samples || [];
        const layers = samples.reduce(
            (all, sample) => all.concat(sample.layers || []), []);
        if (!layers.length) return "Nothing here Plexora can read.";

        //: A run folder is ONE thing the user pointed at, and it has a name
        //: -- so it is named rather than counted. `format` and not `label`,
        //: which already carries a middle dot and would collide with this
        //: line's own.
        const bundles = new Set();
        const files = new Set();
        layers.forEach((layer) => {
            if (layer.bundle) bundles.add(BUNDLE_WORD[layer.bundle.format]
                                          || "a run folder");
            else files.add(layer.src || layer.id);
        });
        const sources = [...bundles];
        if (files.size) {
            sources.push(files.size === 1 ? "1 file" : `${files.size} files`);
        }
        const roles = [];
        const seen = new Set();
        layers.forEach((layer) => {
            const word = layer.reference ? "image" : ROLE_WORD[layer.role];
            if (!word || seen.has(word)) return;
            seen.add(word);
            roles.push(word);
        });

        // Scoped, nothing here is a sample: these are layers of one that
        // already exists, and saying "1 sample" over the + Add Layer dialog
        // would promise a second copy of it.
        const count = scoped()
            ? (layers.length === 1 ? "1 layer" : `${layers.length} layers`)
            : (samples.length === 1 ? "1 sample" : `${samples.length} samples`);
        const from = list(sources);
        const said = list(roles);
        return said ? `${count} from ${from} · ${said}` : `${count} from ${from}`;
    }

    //: How many muted outline colours the cards cycle through -- the
    //: `--plx-sample-tint-N` palette in main.css. Eight, because telling the
    //: ninth card from the first is what the ordinal is for.
    const TINTS = 8;

    /**
     * This sample's outline colour, 0..TINTS-1: handed out in the order cards
     * first appear and then kept for as long as the dialog is open, keyed by
     * what the sample IS. A card that moves up when another is removed keeps
     * its colour -- a colour that followed the position would repaint the
     * card the user was looking at.
     */
    function tintFor(sample) {
        const key = sample.key || sample.name || "";
        if (!(key in state.tints)) {
            state.tints[key] = Object.keys(state.tints).length % TINTS;
        }
        return state.tints[key];
    }

    function renderSample(sample, index, total) {
        const block = el("div", "plx-import-sample");
        // Only with more than one card: an outline is there to say where one
        // sample stops and the next begins, and one card has no neighbour.
        if (!scoped() && total > 1) {
            block.classList.add("is-tinted");
            block.style.setProperty("--plx-sample-tint",
                                    `var(--plx-sample-tint-${tintFor(sample)})`);
        }

        if (!scoped()) block.appendChild(renderCardHead(sample, index, total));

        if (sample.existing && !scoped()) {
            const already = el("div", "plx-import-existing");
            already.appendChild(iconFor("info"));
            already.appendChild(el("span", "plx-import-existing-text",
                `Already imported as ${sample.existing}.`));
            const buttons = el("div", "plx-import-existing-actions");
            // Re-reading the same files can produce a DIFFERENT sample: which
            // of a run's three morphology images Plexora opens, whether a
            // Z-stack is one channel or fourteen and where the pixel size
            // comes from are all answers detection gives, and they improve.
            // Without this the old answer is the only one reachable -- the
            // record is matched by its bundle, so every later import of the
            // same folder lands back on it.
            const again = el("button", "plx-button", "Re-import");
            again.type = "button";
            again.title = `Read these files again and rewrite ${sample.existing}`;
            // This card, not the screen: the other samples on it are not being
            // re-imported and must not be registered by a press on this one.
            again.addEventListener("click", () => submit(sample.existing, index));
            buttons.appendChild(again);
            const openIt = el("button", "plx-button plx-button-primary", "Open it");
            openIt.type = "button";
            openIt.addEventListener("click", () => {
                const target = sample.existing;
                close();
                PlexoraRouter.go(`/${encodeURIComponent(target)}`);
            });
            buttons.appendChild(openIt);
            already.appendChild(buttons);
            block.appendChild(already);
        }

        (sample.layers || []).forEach((layer) => {
            block.appendChild(renderLayer(layer));
            const remoteOptions = renderRemoteOptions(layer);
            if (remoteOptions) block.appendChild(remoteOptions);
            questionsUnder(sample, layer).forEach((question) => {
                block.appendChild(renderQuestion(question, sample));
            });
        });

        questionsFor(sample, "sample").forEach((question) => {
            block.appendChild(renderQuestion(question, sample));
        });

        block.appendChild(renderCardActions(sample));
        if (state.adding && state.adding.key === sample.key) {
            block.appendChild(renderInlinePick(sample));
        }
        return block;
    }

    //: What each missing modality's slot says, and what its row's path box
    //: is called to a screen reader.
    const SLOT = {
        mask: {label: "Add segmentation mask", noun: "the segmentation mask"},
        table: {label: "Add data", noun: "the data"},
        layer: {label: "Add layer", noun: "the layer"},
    };

    /**
     * The card's own actions: what this sample still wants, offered here.
     *
     * A sample is an image and the things registered against it -- a mask, a
     * cell table, whatever else -- and the only way to add any of them used to
     * be a generic "Add files" in the footer, which put the new pick beside
     * the others as a candidate for a sample of its own. A mask added that way
     * routinely became a second card, and the user was left assembling by hand
     * something the screen could simply have offered.
     *
     * Driven by `sample.missing`, so a card that already has both offers only
     * "+ Add layer" and a card with neither offers all three, in the order the
     * record stores them.
     */
    function renderCardActions(sample) {
        const strip = el("div", "plx-import-card-actions");
        const wanted = [...(sample.missing || []), "layer"];
        const buttons = [];
        wanted.forEach((role) => {
            const spec = SLOT[role];
            if (!spec) return;
            const open = state.adding && state.adding.key === sample.key
                         && state.adding.role === role;
            const button = el("button", "plx-import-slot");
            button.type = "button";
            button.dataset.slot = role;
            button.classList.toggle("is-open", Boolean(open));
            button.setAttribute("aria-expanded", open ? "true" : "false");
            // Held while a pick is being read: the row it would open or close
            // is the one showing the wait.
            button.disabled = Boolean(state.addFlight);
            const glyph = el("span", `fas ${open ? "fa-xmark" : "fa-plus"}`);
            glyph.setAttribute("aria-hidden", "true");
            button.appendChild(glyph);
            button.appendChild(el("span", null, spec.label));
            button.addEventListener("click", () => {
                // A second press on the open slot closes it, which is the only
                // way out of a row nobody meant to open.
                state.adding = open ? null : {key: sample.key, role, fresh: true, draft: ""};
                state.addError = null;
                if (open) state.focusAfter = {key: sample.key, role};
                render("proposal");
            });
            buttons.push(button);
            strip.appendChild(button);
        });
        // The keyboard back on this card after its row closed: on the slot
        // that closed, or -- when that slot went because the file filled it --
        // on the first one left.
        const after = state.focusAfter;
        if (after && after.key === sample.key) {
            state.focusAfter = null;
            state.focusTarget = buttons.find((button) => button.dataset.slot === after.role)
                || buttons[0] || null;
        }
        return strip;
    }

    /**
     * A card's "Add …", as ONE row opened under the slot that asked for it:
     *
     *     [Local ▾ | Paste a file or folder path…      ✕] [ Browse ]
     *
     * The card is the context -- which sample the file joins, and the slot
     * already said as what -- so the row repeats none of it: no File | Folder
     * switch, no format chips, no heading. The machine chip is the dialog's
     * one chip, moved in, so whichever machine the slide came from is where
     * this row starts.
     *
     * The right-hand action is one slot with four faces (`data-face`), the
     * same width in each so the path box never jumps:
     *
     *   browse  the box is empty. Browse opens a panel that takes a file or
     *           a folder (browsePicker.js). Whatever comes back is read AT
     *           ONCE -- picking it was the confirmation.
     *   load    a path was typed or pasted. Load, or Enter, reads it.
     *   busy    it is being read: the path stays, read-only, beside a spinner.
     *   retry   it could not be read: the path stays, the reason is under it.
     *
     * When it lands on the card the row closes (`landFlight`). Clearing the
     * box goes back to Browse; Escape clears it, then closes the row.
     */
    function renderInlinePick(sample) {
        const adding = state.adding;
        const spec = SLOT[adding.role] || SLOT.layer;
        const flight = sameIntent(state.addFlight?.intent, adding) ? state.addFlight : null;
        const refused = flight ? null : state.addError;

        const wrap = el("div", "plx-import-inline-pick");
        wrap.dataset.slot = adding.role;
        if (adding.fresh) {
            // Eased in on the render that opens it, and only that one -- the
            // body is rebuilt on every render, and a row that slid in again
            // each time anything changed would never sit still.
            wrap.classList.add("is-entering");
        }

        const row = el("div", "plx-import-compact-row");
        const field = el("div", "plx-import-field");
        if (state.machine) {
            field.appendChild(state.machine.button);
            // Not while a path is being read: the answer is about the
            // machine it was read on.
            state.machine.button.disabled = Boolean(flight);
        }
        const input = el("input", "plx-import-input");
        input.type = "text";
        input.placeholder = "Paste a file or folder path…";
        input.spellcheck = false;
        input.autocomplete = "off";
        input.setAttribute("aria-label", `Path to ${spec.noun}`);
        field.appendChild(input);
        const clear = el("button", "plx-import-compact-clear");
        clear.type = "button";
        clear.title = "Clear";
        clear.setAttribute("aria-label", "Clear the path");
        clear.innerHTML = '<span class="fas fa-xmark" aria-hidden="true"></span>';
        field.appendChild(clear);
        row.appendChild(field);

        const action = el("div", "plx-import-compact-action");
        const split = el("div", "plx-import-compact-split");
        const browse = el("button", "plx-import-compact-browse");
        browse.type = "button";
        browse.title = "Choose a file or a folder";
        browse.innerHTML = '<span class="fas fa-folder-open" aria-hidden="true"></span><span>Browse</span>';
        split.append(browse);
        // Load and Retry are one button: both read the path in the box.
        const load = el("button", "plx-import-compact-load");
        load.type = "button";
        load.innerHTML = `
            <span class="plx-import-compact-word is-load">Load</span>
            <span class="plx-import-compact-word is-retry"><span class="fas fa-rotate-right" aria-hidden="true"></span><span>Retry</span></span>`;
        const busy = el("span", "plx-import-compact-busy");
        busy.innerHTML = '<span class="plx-import-spinner" aria-hidden="true"></span><span>Reading</span>';
        action.append(split, load, busy);
        row.appendChild(action);
        wrap.appendChild(row);

        const setFace = (face) => {
            wrap.dataset.face = face;
            load.title = face === "retry" ? "Read this path again" : "Add this path (Enter)";
        };

        let why = null;
        if (flight) {
            input.value = flight.path;
            input.readOnly = true;
            field.setAttribute("aria-busy", "true");
            wrap.dataset.origin = flight.origin;
            setFace("busy");
            // Once, on the render that starts the wait: a Load pressed by
            // keyboard has just been taken off the screen under its focus.
            if (!flight.focused) {
                flight.focused = true;
                state.focusTarget = input;
            }
        } else if (refused) {
            input.value = refused.typed;
            wrap.classList.add("is-refused");
            if (refused.typed) input.setAttribute("aria-invalid", "true");
            setFace(refused.typed ? "retry" : "browse");
            why = el("p", "plx-import-path-error");
            why.setAttribute("role", "alert");
            const mark = el("span", "fas fa-circle-exclamation");
            mark.setAttribute("aria-hidden", "true");
            why.append(mark, el("span", null, refused.reason));
            wrap.appendChild(why);
            if (!refused.focused) {
                refused.focused = true;
                state.focusTarget = input;
            }
        } else {
            input.value = adding.draft || "";
            setFace(input.value.trim() ? "load" : "browse");
        }
        if (adding.fresh) {
            adding.fresh = false;
            state.focusTarget = input;
        }

        const forgetRefusal = () => {
            if (!state.addError) return;
            state.addError = null;
            wrap.classList.remove("is-refused");
            input.removeAttribute("aria-invalid");
            if (why) why.remove();
            why = null;
        };
        const clearBox = () => {
            input.value = "";
            adding.draft = "";
            forgetRefusal();
            setFace("browse");
            input.focus();
        };
        const take = () => {
            const typed = input.value.trim();
            if (typed) submitSlot(typed, "typed", adding);
        };

        input.addEventListener("input", () => {
            // Only a person typing or pasting reaches here: a browsed path
            // goes straight to `submitSlot`, never through the box's events,
            // so it can never be mistaken for one waiting on Load.
            adding.draft = input.value;
            forgetRefusal();
            setFace(input.value.trim() ? "load" : "browse");
        });
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                if (!input.readOnly) take();
            } else if (event.key === "Escape" && !input.readOnly) {
                // Scoped with nothing read yet, the row IS the dialog: there
                // is no card to go back to, and the dialog's own Escape
                // closing it is what an empty box asks for.
                if (!input.value && pickPhase() === "pick") return;
                // Otherwise the dialog's own Escape would close the whole dialog.
                event.preventDefault();
                event.stopPropagation();
                if (input.value) {
                    clearBox();
                } else {
                    state.adding = null;
                    state.addError = null;
                    state.focusAfter = {key: adding.key, role: adding.role};
                    render("proposal");
                }
            }
        });
        clear.addEventListener("click", clearBox);
        load.addEventListener("click", take);
        browse.addEventListener("click", () => pickWith("any"));

        // The service's own line -- "Sharing…", "Not connected to hpc yet" --
        // under the row it is about. Empty, it takes no room.
        if (state.machine) wrap.appendChild(state.machine.status);

        // Small tables dropped on the row, as on the pick state's control.
        wrap.addEventListener("dragover", (event) => {
            event.preventDefault();
            wrap.classList.add("is-over");
        });
        wrap.addEventListener("dragleave", (event) => {
            if (!wrap.contains(event.relatedTarget)) wrap.classList.remove("is-over");
        });
        wrap.addEventListener("drop", onDrop);

        //: Held so a second press while the system's file browser is opening
        //: cannot open a second one.
        state.pickControls = [browse];
        state.pickFocus = browse;
        state.pickInput = input;
        return wrap;
    }

    // -- a row's menu ---------------------------------------------------------

    function browseMenuOpen() {
        return Boolean(state && state.browseMenu && !state.browseMenu.menu.hidden);
    }

    /**
     * A small menu under one control of a row -- a role badge's readings.
     * Fixed-positioned in the dialog rather than inside the card, which the
     * scrolling body would clip; right-aligned to what it opened from, flipped
     * above it when there is no room below.
     */
    function openRowMenu(trigger, alignTo, items, label) {
        if (!state || browseMenuOpen() || trigger.disabled) return;
        closeMenu(false);
        if (!state.browseMenu) state.browseMenu = buildBrowseMenu();
        const browse = state.browseMenu;
        browse.anchor = trigger;
        browse.menu.setAttribute("aria-label", label || "Options");
        browse.menu.replaceChildren(...items);
        browse.menu.hidden = false;
        const rect = (alignTo || trigger).getBoundingClientRect();
        const spread = browse.menu.getBoundingClientRect();
        let top = rect.bottom + 6;
        if (top + spread.height > window.innerHeight - 8 && rect.top > spread.height + 8) {
            top = rect.top - spread.height - 6;
        }
        browse.menu.style.left = `${Math.round(Math.max(8, rect.right - spread.width))}px`;
        browse.menu.style.top = `${Math.round(top)}px`;
        trigger.setAttribute("aria-expanded", "true");
        browse.menu.querySelector(".plx-import-menu-item")?.focus();
    }

    function closeBrowseMenu(refocus) {
        const browse = state && state.browseMenu;
        if (!browse || browse.menu.hidden) return;
        browse.menu.hidden = true;
        const anchor = browse.anchor;
        browse.anchor = null;
        if (anchor) {
            anchor.setAttribute("aria-expanded", "false");
            if (refocus && anchor.isConnected) anchor.focus();
        }
    }

    function buildBrowseMenu() {
        const menu = el("div", "plx-import-menu plx-import-browse-menu");
        menu.setAttribute("role", "menu");
        menu.hidden = true;
        menu.addEventListener("keydown", (event) => {
            const items = Array.from(menu.querySelectorAll(".plx-import-menu-item"));
            const at = items.indexOf(document.activeElement);
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                closeBrowseMenu(true);
            } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                const step = event.key === "ArrowDown" ? 1 : -1;
                items[(at + step + items.length) % items.length]?.focus();
            } else if (event.key === "Home" || event.key === "End") {
                event.preventDefault();
                items[event.key === "Home" ? 0 : items.length - 1]?.focus();
            } else if (event.key === "Tab") {
                closeBrowseMenu(false);
            }
        });
        dialog.appendChild(menu);
        return {menu, anchor: null};
    }

    /**
     * The card's title line: which sample, what it is called, where it goes.
     *
     * Name and Dataset used to be a labelled input and a full-width button in
     * a two-column grid at the foot of every card -- two form fields, drawn at
     * the weight of the thing being imported, for two answers that arrive
     * already filled in. They are metadata about a sample, so they are drawn
     * as metadata: muted text that becomes editable where it stands. The
     * ordinal is only there when there is more than one card, because "Sample
     * 1" over the only sample on screen is a number with nothing to tell apart.
     */
    function renderCardHead(sample, index, total) {
        const head = el("div", "plx-import-card-head");
        if (total > 1) {
            head.appendChild(el("span", "plx-import-card-ordinal",
                                `Sample ${index + 1}`));
            head.appendChild(el("span", "plx-import-card-dot", "·"));
        }

        const meta = state.meta[sample.key] || {};
        const name = el("button", "plx-import-name-inline",
                        meta.name || sample.name || "Untitled");
        name.type = "button";
        name.title = "Click to rename";
        name.addEventListener("click", () => editName(name, sample));
        head.appendChild(name);

        head.appendChild(el("span", "plx-import-card-dot", "·"));
        const dataset = el("button", "plx-import-dataset-inline");
        dataset.type = "button";
        dataset.title = "Click to choose a dataset";
        dataset.appendChild(el("span", null, datasetFor(sample)?.name
                                             || "No dataset"));
        const chevron = el("span", "fas fa-chevron-down");
        chevron.setAttribute("aria-hidden", "true");
        dataset.appendChild(chevron);
        dataset.addEventListener("click", () => chooseDataset(sample));
        head.appendChild(dataset);
        // Only where it would change something: this card's dataset is not
        // what every other card has.
        const others = state.proposal?.samples || [];
        if (total > 1 && others.some(
                (other) => !sameDataset(datasetFor(other), datasetFor(sample)))) {
            const all = el("button", "plx-import-dataset-all", "Use for all");
            all.type = "button";
            all.title = `Put every sample in ${datasetFor(sample)?.name || "no dataset"}`;
            all.addEventListener("click", () => useForAll(datasetFor(sample)));
            head.appendChild(all);
        }
        return head;
    }

    /** This sample's dataset: its own answer, or the dialog's default. */
    function datasetFor(sample) {
        const meta = state.meta[sample.key];
        return meta && "dataset" in meta ? meta.dataset : state.dataset;
    }

    /**
     * The muted name, edited where it stands.
     *
     * An input with the same metrics as the text it replaces, so nothing moves
     * when it appears -- which is what makes it read as the same line rather
     * than as a form opening. Enter and blur commit; Escape puts back what was
     * there, because a name typed over by accident is otherwise unrecoverable
     * once the original is gone.
     */
    function editName(button, sample) {
        const before = button.textContent;
        const box = el("input", "plx-import-name-edit");
        box.type = "text";
        box.value = before === "Untitled" ? "" : before;
        box.spellcheck = false;
        box.setAttribute("aria-label", "Sample name");
        let settled = false;
        const commit = (keep) => {
            if (settled) return;
            settled = true;
            const typed = box.value.trim();
            if (keep && typed && typed !== before) {
                state.meta[sample.key] = Object.assign(
                    {}, state.meta[sample.key], {name: typed});
            }
            button.textContent = (keep && typed) ? typed : before;
            box.replaceWith(button);
            button.focus();
        };
        box.addEventListener("keydown", (event) => {
            if (event.key === "Enter") { event.preventDefault(); commit(true); }
            else if (event.key === "Escape") { event.preventDefault(); commit(false); }
        });
        box.addEventListener("blur", () => commit(true));
        button.replaceWith(box);
        box.focus();
        box.select();
    }

    function questionsFor(sample, scope) {
        return (sample.questions || []).filter((q) => q.scope === scope);
    }

    /**
     * The questions drawn under one row. By scope, except where two rows share
     * an id -- a folder's two cell tables are both `table` -- and then each
     * question goes under the row whose `needs` hold it, not under both.
     */
    function questionsUnder(sample, layer) {
        return questionsFor(sample, `layer:${layer.id}`).filter((question) => {
            const owner = (sample.layers || []).find(
                (row) => (row.needs || []).indexOf(question.id) !== -1);
            return !owner || owner === layer;
        });
    }

    function iconFor(kind) {
        const wrap = el("span", "plx-import-row-icon");
        const glyph = el("span", `fas ${KIND_ICON[kind] || "fa-circle-question"}`);
        glyph.setAttribute("aria-hidden", "true");
        wrap.appendChild(glyph);
        return wrap;
    }

    /**
     * `{full}`, the path of the file a row came from, or null when the row's
     * own label already is that filename, or the row came out of a run folder
     * (whose files are the run's, and named by it). Read from the PICK when
     * `src` is not a local path: a node file's `src` is a derived resource id.
     * A local `src` is the file itself, and the pick may be the FOLDER it was
     * found in -- two tables out of one folder both read "…/LSP11385", which
     * looked like one table listed twice.
     */
    function fileHint(layer) {
        if (layer.bundle) return null;
        const src = String(layer.src || "");
        const local = src && !src.startsWith("node://")
            && !(window.PlexoraLocators && window.PlexoraLocators.isRemoteLocator(src));
        const full = local ? src : String(pickOf(layer) || src);
        if (!full) return null;
        const parts = full.replace(/\\/g, "/").replace(/\/+$/, "").split("/");
        const name = parts[parts.length - 1];
        if (!name || name === layer.label) return null;
        return {full};
    }

    function renderLayer(layer) {
        const row = el("div", "plx-import-row");
        row.dataset.layer = layer.id;
        row.appendChild(iconFor(layer.kind));

        const text = el("div", "plx-import-row-text");
        const nameLine = el("span", "plx-import-row-name", layer.label || layer.id);
        if (window.PlexoraLocators && window.PlexoraLocators.isRemoteLocator(layer.src)) {
            // Read from a web address rather than this disk -- worth a glance
            // on a screen that is otherwise all local files, and not a
            // warning: an https layer is exactly as read-only-safe as a local
            // one, only slower the first time.
            // An `sftp://` layer streams from a files-only server rather than
            // the web, and is named for what it is.
            const sftp = window.PlexoraLocators.remoteScheme(layer.src) === "sftp";
            nameLine.appendChild(el("span", "plx-import-badge is-remote",
                                    sftp ? "SFTP" : "Web"));
        }
        text.appendChild(nameLine);
        text.appendChild(el("span", "plx-import-row-detail", layer.detail || ""));
        // WHICH file, when the label is a role rather than a filename: two
        // cards that each say "Segmentation mask" give no way to check that
        // sample 2's mask is sample 2's. On hover of the name, not as a line
        // of its own: a path under every mask and table was a third line of
        // text per row that almost nobody needed to read.
        const file = fileHint(layer);
        if (file) nameLine.title = file.full;
        if (layer.dependency && layer.dependency.install) {
            // A package this environment has not got. The row stays -- what is
            // missing is an install, not the data -- and the command to fix it
            // is shown rather than a stack trace after the fact.
            const hint = el("code", "plx-import-install", layer.dependency.install);
            text.appendChild(hint);
        }
        row.appendChild(text);
        // The reference is the image everything else is registered against, so
        // it says so rather than saying "image": which of three is the frame
        // is the one thing on this screen worth correcting before importing.
        row.appendChild(roleBadge(layer));
        const pick = pickOf(layer);
        if (pick) row.appendChild(removeButton(pick));
        // Just added from this card's row, which has closed: a brief tint so
        // the eye lands on where the file went rather than on the gap it left.
        if (pick && pick === state.arrived) row.classList.add("is-arrived");
        return row;
    }

    //: What each reading on a row's role menu says under its name: what the
    //: file will be drawn as, which is the difference that matters.
    const SWITCH_HINT = {
        image: "Intensities, drawn as a channel",
        mask: "Cell ids, drawn as outlines",
        none: "Leave this file out",
    };

    /**
     * The row's role, ranged right. A plain label where the file has one
     * reading; a menu where it has another (`layer.switch`), so a detection
     * that got it wrong is one click to correct -- for a confident answer as
     * much as an uncertain one. A confident mask used to have no way back
     * short of removing the file, and re-adding it read it as a mask again.
     */
    function roleBadge(layer) {
        const text = layer.reference ? "Reference image"
                                     : ROLE_BADGE[layer.role] || "Layer";
        const choice = layer.switch;
        if (!choice || (choice.options || []).length < 2) {
            return el("span", "plx-import-role", text);
        }
        const button = el("button", "plx-import-role is-switch");
        button.type = "button";
        button.title = "Change what this file is";
        button.disabled = Boolean(state.addFlight);
        button.setAttribute("aria-haspopup", "menu");
        button.setAttribute("aria-expanded", "false");
        button.setAttribute("aria-label",
            `${text}. Change what ${layer.label || "this file"} is`);
        button.appendChild(el("span", null, text));
        const chevron = el("span", "fas fa-chevron-down plx-import-role-chevron");
        chevron.setAttribute("aria-hidden", "true");
        button.appendChild(chevron);
        const show = () => openRowMenu(button, button, choice.options.map((option) => {
            const reading = option.value === "none" ? "none"
                : option.value === "image" ? "image" : "mask";
            return menuItem({
                icon: {none: "ban", image: "layer-group", mask: "shapes"}[reading],
                name: option.label,
                sub: SWITCH_HINT[reading],
                role: "menuitemradio",
                checked: option.value === choice.value,
                onPick: () => {
                    closeBrowseMenu(false);
                    // Re-inspected, not patched: a mask that becomes an image
                    // can become the reference, and only the server knows.
                    if (option.value !== choice.value) answer(choice.question, option.value);
                    else button.focus();
                },
            });
        }), `What ${layer.label || "this file"} is`);
        button.addEventListener("click", () => {
            if (browseMenuOpen() && state.browseMenu.anchor === button) closeBrowseMenu(true);
            else show();
        });
        button.addEventListener("keydown", (event) => {
            if (event.key === "ArrowDown" && !browseMenuOpen()) {
                event.preventDefault();
                show();
            }
        });
        return button;
    }

    //: Schemes a bucket sits under -- the ones that can need an endpoint, an
    //: anonymous read or a named profile before they will open at all. `https`
    //: is deliberately not here: it is either public or it is not reachable,
    //: and there is no field on this form that changes which.
    const CONFIGURABLE_REMOTE = ["s3", "gs", "gcs", "az", "abfs", "abfss"];

    /**
     * "Set endpoint / access…", collapsed under a layer read from a bucket.
     *
     * The same options Settings > Web data keeps, reached from the moment
     * they are first needed instead of asking somebody to leave the dialog,
     * remember which bucket failed, and go find the address book. Saving
     * re-runs `inspect()`, which is the point: a store that could not be read
     * for want of a credential is read again with the one just given it.
     *
     * Returns null for anything this has nothing to offer -- a local file, a
     * `node://` locator, an `https` layer, or a bucket URL with no host to
     * key a prefix by.
     */
    function renderRemoteOptions(layer) {
        const locators = window.PlexoraLocators;
        const scheme = locators && locators.remoteScheme(layer.src);
        if (!scheme || CONFIGURABLE_REMOTE.indexOf(scheme) === -1) return null;
        let host = "";
        try { host = new URL(layer.src).host; } catch (error) { /* no prefix to key by */ }
        if (!host) return null;
        const prefix = `${scheme}://${host}/`;

        const row = el("div", "plx-import-question");
        const label = el("div", "plx-import-question-label");
        const toggle = el("button", "plx-import-formats-link", "Set endpoint / access…");
        toggle.type = "button";
        toggle.setAttribute("aria-expanded", "false");
        label.appendChild(toggle);
        row.appendChild(label);

        const form = el("div", "plx-import-remote-form");
        form.hidden = true;

        const endpointField = el("label", "plx-import-remote-field");
        endpointField.appendChild(el("span", null, "Endpoint URL"));
        const endpoint = el("input", "plx-import-path-input");
        endpoint.type = "text";
        endpoint.placeholder = "https://s3.example.org (leave blank for AWS)";
        endpoint.spellcheck = false;
        endpointField.appendChild(endpoint);
        form.appendChild(endpointField);

        const anonField = el("label", "plx-import-remote-check");
        const anon = document.createElement("input");
        anon.type = "checkbox";
        anon.checked = true;
        anonField.appendChild(anon);
        anonField.appendChild(el("span", null, "Anonymous — no credentials needed"));
        form.appendChild(anonField);

        const profileField = el("label", "plx-import-remote-field");
        profileField.appendChild(el("span", null, "Profile"));
        const profile = el("input", "plx-import-path-input");
        profile.type = "text";
        profile.placeholder = "default";
        profileField.appendChild(profile);
        form.appendChild(profileField);

        // Disabled rather than hidden: an anonymous read has no profile, and a
        // field that vanished when it stopped applying would move everything
        // under it -- this only greys out, in place.
        function syncProfile() {
            profile.disabled = anon.checked;
            profileField.classList.toggle("is-disabled", anon.checked);
        }
        anon.addEventListener("change", syncProfile);
        syncProfile();

        form.appendChild(el("p", "plx-import-question-hint",
            "Options for private buckets or S3-compatible servers. Keys are "
            + "never stored here — Plexora uses your AWS/Google/Azure "
            + "credentials from the environment."));

        const error = el("p", "plx-import-remote-error");
        error.hidden = true;
        form.appendChild(error);

        const save = el("button", "plx-button plx-button-primary", "Save");
        save.type = "button";
        save.addEventListener("click", async () => {
            error.hidden = true;
            save.disabled = true;
            try {
                const response = await fetch(plexoraUrl("settings/webdata/options"), {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        prefix,
                        endpoint_url: endpoint.value.trim() || undefined,
                        anon: anon.checked,
                        profile: anon.checked ? undefined : (profile.value.trim() || undefined),
                    }),
                });
                const result = await response.json();
                if (!response.ok || result.error) {
                    throw new Error(result.error || "Could not save that.");
                }
                inspect();
            } catch (err) {
                error.textContent = err.message || "Could not save that.";
                error.hidden = false;
            } finally {
                save.disabled = false;
            }
        });
        form.appendChild(save);

        // Filled in from the address book the first time this is opened --
        // not on every render, which would be one request per bucket layer on
        // a screen that may have several.
        let filled = false;
        toggle.addEventListener("click", () => {
            form.hidden = !form.hidden;
            toggle.setAttribute("aria-expanded", String(!form.hidden));
            if (!form.hidden && !filled) {
                filled = true;
                prefillRemoteOptions(prefix, {endpoint, anon, profile, syncProfile});
            }
        });

        row.appendChild(form);
        return row;
    }

    /** Fill a just-opened access form from the longest address-book prefix
     *  this layer's own prefix starts with -- the same rule the server
     *  applies when it reads the book back for an open. Left blank on any
     *  failure, which is exactly what an unopened form already looks like. */
    async function prefillRemoteOptions(prefix, fields) {
        try {
            const response = await fetch(plexoraUrl("settings/webdata/options"));
            const body = await response.json();
            const options = body.options || [];
            let best = null;
            options.forEach((option) => {
                const saved = String(option.prefix || "");
                if (saved && prefix.startsWith(saved)
                    && (!best || saved.length > best.prefix.length)) {
                    best = option;
                }
            });
            if (!best) return;
            fields.endpoint.value = best.endpoint_url || "";
            // Absent means it was never set, which this form treats the same
            // as ticked -- see the callout's own default.
            fields.anon.checked = best.anon !== false;
            fields.profile.value = best.profile || "";
            fields.syncProfile();
        } catch (error) { /* offered empty, which is what a blank form is */ }
    }

    /**
     * The path a row came from, as the caller itself sent it.
     *
     * NOT `layer.src`, and that was the bug behind "Remove does nothing".
     * A row's `src` is where the data will be READ from, which is not the
     * string that was picked: a browsed path on a node comes back as
     * `node://o2/<derived resource id>`, and a pasted `~/slide.tif` comes back
     * expanded. Filtering the pick list by `src` matched neither, so the ✕ on
     * a remote image ran, removed nothing, re-inspected, and drew the same row
     * again.
     *
     * `pick` is the index of the entry in the array that produced this
     * proposal, which `state.picked` holds -- so a bundle's six rows all
     * resolve to the one folder that was picked, and removing any of them
     * removes the run.
     */
    function pickOf(row) {
        if (!row || row.pick === null || row.pick === undefined) return null;
        return state.picked[row.pick] ?? null;
    }

    function removeButton(pick) {
        const button = el("button", "plx-import-remove");
        button.type = "button";
        button.title = "Leave this out";
        button.setAttribute("aria-label", "Leave this out");
        button.innerHTML = '<span class="fas fa-xmark" aria-hidden="true"></span>';
        button.addEventListener("click", () => removePick(pick));
        return button;
    }

    /**
     * Take one pick back out, and forget everything that was said about it.
     *
     * The answers go with it, or a file removed and picked again arrives
     * carrying the role and the sample it had last time -- including a
     * `sample-for` naming a card that may no longer exist.
     */
    function removePick(pick) {
        if (!pick) return;
        forgetAbout(pick);
        state.picks = state.picks.filter((entry) => entry !== pick);
        release([pick]);
        if (!state.picks.length) {
            state.proposal = null;
            state.picked = [];
            state.adding = null;
            render("pick");
            return;
        }
        inspect();
    }

    /**
     * One question, attached under the row it is about.
     *
     * A callout rather than an indented paragraph: a question here is a
     * failure of DETECTION, and it has to be findable among eight rows without
     * being alarming. It is never required -- `part("go")` is gated on layers
     * alone -- so the unanswered state is a warning-coloured rule and a word,
     * not a barrier.
     */
    function renderQuestion(question, sample) {
        // A confirmation is asked as a modal when Import is pressed, so under
        // the row it is only a note saying it will be -- or what was said.
        if (question.kind === "confirm") return renderConfirmNote(question, sample);
        const current = state.answers[question.id] ?? question.default;
        // The one question with no default: a store with several tables,
        // where guessing would silently give you a different set of cells.
        // Said out loud, with what happens if it is left alone.
        const unanswered = current === null || current === undefined;
        if (!unanswered && state.settled[question.id]) {
            return renderSettled(question, current);
        }
        const row = el("div", "plx-import-question");
        row.classList.toggle("is-unanswered", unanswered);

        const label = el("label", "plx-import-question-label", question.label);
        if (unanswered) {
            label.appendChild(el("span", "plx-import-badge", "unanswered"));
        }
        row.appendChild(label);

        if (question.kind === "select" || (question.options || []).length > 3) {
            const select = el("select", "plx-import-question-select");
            if (unanswered) {
                const placeholder = el("option", null, "Choose…");
                placeholder.value = "";
                placeholder.disabled = true;
                placeholder.selected = true;
                select.appendChild(placeholder);
            }
            (question.options || []).forEach((option) => {
                const item = el("option", null, option.label);
                item.value = option.value;
                if (option.value === current) item.selected = true;
                select.appendChild(item);
            });
            select.addEventListener("change", () => answer(question.id, select.value));
            label.htmlFor = select.id = `q_${cssId(question.id)}`;
            const control = el("div", "plx-import-question-control");
            control.appendChild(select);
            if (!unanswered) control.appendChild(settleButton(question, () => select.value));
            row.appendChild(control);
        } else {
            const group = el("div", "plx-import-question-choices");
            (question.options || []).forEach((option) => {
                const button = el("button", "plx-import-choice");
                button.type = "button";
                const chosen = option.value === current;
                // A dot rather than colour alone, so which one is chosen
                // survives a colour-blind reader and a screenshot.
                const dot = el("span", `fas ${chosen ? "fa-circle-dot" : "fa-circle"}`);
                dot.setAttribute("aria-hidden", "true");
                button.appendChild(dot);
                button.appendChild(el("span", null, option.label));
                button.classList.toggle("is-chosen", chosen);
                button.setAttribute("aria-pressed", chosen ? "true" : "false");
                button.addEventListener("click", () => answer(question.id, option.value));
                group.appendChild(button);
            });
            if (!unanswered) group.appendChild(settleButton(question, () => current));
            row.appendChild(group);
        }
        if (unanswered) {
            row.appendChild(el("p", "plx-import-question-hint",
                "The import goes ahead either way — Plexora asks again when a "
                + "tool needs it."));
        }
        return row;
    }

    /**
     * ✓ beside a question's control: "what it shows is right". Records what
     * is showing as the answer -- a default nobody confirmed is written down
     * as `unresolved` at registration, and a confirmed one is not -- and puts
     * the question away (`renderSettled`). Nothing to re-inspect: a default
     * is applied whether or not it is answered, so saying it changes no row.
     */
    function settleButton(question, read) {
        const button = el("button", "plx-import-settle");
        button.type = "button";
        button.title = "Use this";
        button.setAttribute("aria-label", `Use this: ${question.label}`);
        button.innerHTML = '<span class="fas fa-check" aria-hidden="true"></span>';
        button.addEventListener("click", () => {
            state.answers[question.id] = read();
            state.settled[question.id] = true;
            render("proposal");
        });
        return button;
    }

    /**
     * A question its ✓ settled: one line saying what was chosen, and the way
     * back to the control. The pyramid note's answered state, for the same
     * reason -- a decision taken should stop taking a callout's room.
     */
    function renderSettled(question, current) {
        const row = el("div", "plx-import-question is-confirm is-settled");
        const head = el("div", "plx-import-question-label");
        const mark = el("span", "fas fa-check plx-import-settled-mark");
        mark.setAttribute("aria-hidden", "true");
        head.appendChild(mark);
        head.appendChild(el("span", null, question.label));
        const chosen = (question.options || []).find((option) => option.value === current);
        head.appendChild(el("span", "plx-import-settled-value",
            chosen ? chosen.label : String(current)));
        const change = el("button", "plx-import-link", "Change…");
        change.type = "button";
        change.addEventListener("click", () => {
            delete state.settled[question.id];
            render("proposal");
        });
        head.appendChild(change);
        row.appendChild(head);
        return row;
    }

    /** The row a layer-scoped question is about, or null. */
    function layerOf(sample, question) {
        const id = String(question.scope || "").replace(/^layer:/, "");
        return (sample?.layers || []).find((layer) => layer.id === id) || null;
    }

    /** "1.4 GB", or "" when the server did not say. */
    function formatBytes(count) {
        const value = Number(count);
        if (!Number.isFinite(value) || value <= 0) return "";
        const units = ["bytes", "KB", "MB", "GB", "TB"];
        let size = value;
        let unit = 0;
        while (size >= 1024 && unit < units.length - 1) {
            size /= 1024;
            unit += 1;
        }
        return `${size >= 10 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
    }

    function formatCount(value) {
        const number = Number(value);
        return Number.isFinite(number) ? number.toLocaleString() : "?";
    }

    /**
     * What a pyramidize confirmation says under its row. Read-only: the
     * question is put as a modal at Import (`askConfirmations`), so this only
     * says that it will be -- or, once answered, what was said, with a way
     * to change it. Never `is-unanswered`, and never holds the import up.
     */
    function renderConfirmNote(question, sample) {
        const row = el("div", "plx-import-question is-confirm");
        const pyramid = layerOf(sample, question)?.render?.pyramid || {};
        const said = state.answers[question.id];
        const line = el("p", "plx-import-question-hint");
        if (said === undefined) {
            const size = formatBytes(pyramid.output_bytes_estimate);
            line.textContent = "Not pyramidized — Plexora will offer to build a "
                + `pyramidized copy${size ? ` (about ${size})` : ""} when you `
                + "press Import.";
            row.appendChild(line);
            return row;
        }
        const head = el("div", "plx-import-question-label");
        head.appendChild(el("span", "plx-import-badge",
            said === "no" ? "as is" : "will pyramidize"));
        head.appendChild(el("span", null, said === "no"
            ? "Opens the original, without a pyramid."
            : `Builds ${pyramid.output_name || "a pyramidized copy"} first.`));
        const change = el("button", "plx-import-link", "Change…");
        change.type = "button";
        change.addEventListener("click", async () => {
            const value = await askPyramid(question, sample);
            if (value === null) return;
            state.answers[question.id] = value;
            // Local: the answer changes what Import will do, not what the
            // files are, so there is nothing to re-inspect.
            render("proposal");
        });
        head.appendChild(change);
        row.appendChild(head);
        return row;
    }

    /**
     * Put one pyramidize confirmation to the user. Resolves the chosen value
     * ("yes" / "no"), or null when the modal is dismissed.
     */
    async function askPyramid(question, sample) {
        const layer = layerOf(sample, question);
        const pyramid = layer?.render?.pyramid || {};
        const name = layer?.label || basename(layer?.src) || "This image";
        const options = question.options || [];
        const yes = options.find((option) => option.value === "yes")
            || {value: "yes", label: "Pyramidize and import"};
        const no = options.find((option) => option.value === "no")
            || {value: "no", label: "Import as is"};
        if (!window.PlexoraConfirm?.choose) return question.default ?? yes.value;
        const facts = [pyramid.dtype, formatBytes(pyramid.bytes)].filter(Boolean);
        const size = formatBytes(pyramid.output_bytes_estimate);
        const copy = pyramid.output_name || "a .pyramid.ome.tiff file";
        return window.PlexoraConfirm.choose({
            title: "This image is not pyramidized",
            body: [
                `${name} is ${formatCount(pyramid.width)} × ${formatCount(pyramid.height)} px `
                + `with ${formatCount(pyramid.channels)} channels`
                + (facts.length ? ` (${facts.join(", ")})` : "")
                + " and has no reduced-resolution levels.",
                "Opening it at full resolution directly can need more memory "
                + "than this machine has, and some readers refuse it — the "
                + "viewer may stall or fail to open it.",
                "Plexora can write a pyramidized copy once"
                + (size ? ` — about ${size}` : "")
                + (pyramid.levels ? `, ${pyramid.levels} levels` : "")
                + `, saved as ${copy} ${pyramid.location || "beside the original"}`
                + " — and open that instead. The original is not changed. Each "
                + "step is shown as it runs.",
            ],
            choices: [
                {value: no.value, label: no.label},
                {value: yes.value, label: yes.label, kind: "primary", focus: true},
            ],
        });
    }

    /**
     * Every pyramidize confirmation the samples about to be imported still
     * owe, asked one at a time. False when one was dismissed: nothing is
     * imported and nothing about the answers changes.
     */
    async function askConfirmations(targets) {
        for (const {sample} of targets) {
            for (const question of sample.questions || []) {
                if (question.kind !== "confirm") continue;
                if (state.answers[question.id] !== undefined) continue;
                const value = await askPyramid(question, sample);
                if (value === null || !state) return false;
                state.answers[question.id] = value;
            }
        }
        return true;
    }

    /** Whether this row's import will build a pyramidized copy. */
    function buildsPyramid(sample, layer) {
        if (!layer?.render?.pyramid) return false;
        const question = questionsFor(sample, `layer:${layer.id}`)
            .find((candidate) => candidate.kind === "confirm");
        if (!question) return false;
        return (state.answers[question.id] ?? question.default) !== "no";
    }

    //: "a, b and c". Its own function because both halves of the summary line
    //: need it and an Oxford-comma-less list is the one bit of prose here.
    function list(items) {
        if (items.length < 2) return items[0] || "";
        return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
    }

    function cssId(value) {
        return String(value).replace(/[^\w-]/g, "_");
    }

    function answer(id, value) {
        state.answers[id] = value;
        // Re-inspected rather than patched locally: an answer can change what
        // the other rows ARE -- picking a different reference re-registers
        // every layer against it -- and the server is the only thing that
        // knows that.
        inspect();
    }

    /**
     * Where this sample goes in the library -- and, the first time, where
     * every sample goes.
     *
     * Importing twelve slides into one dataset used to take twelve trips
     * through this picker. So the FIRST dataset chosen in the dialog, on any
     * card, becomes the default: every card that has not been set by hand
     * takes it, and so does every sample added after it. From then on a
     * choice on a card is that card's alone -- a default, never a lock -- and
     * a card that differs from the others offers "Use for all" beside it,
     * which is also the way to correct a first choice that was wrong.
     */
    async function chooseDataset(sample) {
        let datasets = [];
        try {
            const response = await fetch(plexoraUrl("datasets"));
            const payload = await response.json();
            datasets = payload.datasets || [];
        } catch (error) { /* offered without the list */ }
        const samples = state.proposal?.samples || [sample];
        const shared = !state.datasetSaid;
        const picked = await window.PlexoraDatasetPicker.choose({
            datasets,
            count: 1,
            allowRoot: true,
            allowNew: true,
            // Says which is about to happen, because the first choice moves
            // cards the user is not looking at.
            title: shared && samples.length > 1
                ? `Put these ${samples.length} samples in…`
                : "Put this sample in…",
            rootLabel: "No dataset",
        });
        if (!picked || !state) return;
        let chosen = null;
        if (picked.kind === "root") chosen = null;
        else if (picked.kind === "new") chosen = {new: picked.name, name: picked.name};
        else {
            const found = datasets.find((d) => d.id === picked.id);
            chosen = {id: picked.id, name: found?.name || picked.id};
        }
        state.datasetSaid = true;
        if (shared) useForAll(chosen);
        else {
            state.meta[sample.key] = Object.assign(
                {}, state.meta[sample.key], {dataset: chosen});
            render("proposal");
        }
    }

    /**
     * One dataset for every card and every sample added later: the default
     * replaced, and each card's own choice forgotten so it follows.
     */
    function useForAll(dataset) {
        state.dataset = dataset;
        Object.keys(state.meta).forEach((key) => {
            if (!("dataset" in state.meta[key])) return;
            const {dataset: _, ...rest} = state.meta[key];
            state.meta[key] = rest;
        });
        render("proposal");
    }

    /** Two dataset answers name the same place: both none, or one id/new name. */
    function sameDataset(left, right) {
        if (!left || !right) return !left && !right;
        return (left.id ?? null) === (right.id ?? null)
            && (left.new ?? null) === (right.new ?? null);
    }

    // -- state 3: importing -------------------------------------------------

    /**
     * Register the samples these picks make. One POST each.
     *
     * `/import/sample` registers ONE sample -- `proposal.samples[index]` --
     * and this used to post once whatever was on screen, so "Import 3 samples"
     * created one project and quietly dropped the other two. Each POST carries
     * its sample's `key` beside the index, so a request composed against an
     * older reading of the files is refused rather than registering whichever
     * sample now happens to sit at that position.
     *
     * @param replace - the name of an existing sample this rewrites. Sent as
     *   the NAME as well, because a name and a replacement that disagree are
     *   a request for a second copy under a deduplicated name -- which is the
     *   opposite of what "Re-import" means.
     * @param only - the index of the single sample to register. The Re-import
     *   button acts on the card it is on, not on the screen.
     */
    async function submit(replace, only) {
        if (!state || !state.picks.length) return;
        if (scoped()) return submitScoped();

        // No proposal yet means Import was pressed before the first inspection
        // landed. One POST at index 0 and no key, which is what this did
        // before it knew about several samples -- and the server re-inspects
        // anyway, so the files still decide.
        const samples = state.proposal?.samples || [{key: "", name: ""}];
        const targets = (only === null || only === undefined)
            ? samples.map((sample, at) => ({sample, at}))
            : [{sample: samples[only], at: only}];
        if (!targets.length || !targets[0].sample) return;
        // Before anything is written, and before the dialog changes: a
        // dismissed modal leaves the proposal exactly as it was.
        if (!(await askConfirmations(targets))) {
            part("go").focus();
            return;
        }

        render("importing");
        setStatus(replace ? `Re-reading ${replace}…`
            : targets.length > 1 ? `Registering ${targets.length} samples…`
            : "Registering…");
        const results = [];
        const resultAt = [];
        for (const {sample, at} of targets) {
            const meta = state.meta[sample.key] || {};
            const dataset = datasetFor(sample);
            let result = null;
            // The POST does the slow part -- tiling the image, converting a
            // 10x matrix -- before the sample has a name to poll by, so the
            // request names itself and the rail reads that while it runs.
            const token = `imp-${Date.now().toString(36)}-`
                + Math.random().toString(36).slice(2, 10);
            const stop = watchRegistration(token, at);
            // The first poll is REGISTER_POLL_MS away, and a pyramid build
            // starts the moment the POST lands -- so the row says so now.
            (sample.layers || []).forEach((layer) => {
                if (!buildsPyramid(sample, layer)) return;
                paintLine(state.bars?.get(`${at}:${layer.id}`), {
                    status: "pending", stage: "registering", substage: "opening",
                    stage_label: "Opening the source image", progress: 0,
                });
            });
            try {
                const response = await fetch(plexoraUrl("import/sample"), {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        paths: state.picks,
                        answers: state.answers,
                        index: at,
                        key: sample.key || undefined,
                        name: replace || meta.name || undefined,
                        replace: replace || undefined,
                        dataset: dataset || undefined,
                        token,
                    }),
                });
                result = await readJson(response);
                stop();
                if (response.status === 409 && result.suggestion) {
                    // The name is taken. Offered rather than silently renamed:
                    // somebody who typed a name meant it, and quietly filing
                    // their import under `melanoma_2` is how two copies of one
                    // slide happen. Stored against the sample it belongs to,
                    // so it is the line the user reads that changes.
                    state.meta[sample.key] = Object.assign(
                        {}, state.meta[sample.key], {name: result.suggestion});
                    render("proposal");
                    setStatus(`${result.error} Using ${result.suggestion} `
                              + "instead — edit the name if you want another.",
                              true);
                    return;
                }
                if (!response.ok) throw new Error(result.error || "Import failed.");
            } catch (error) {
                stop();
                // A pyramid build can take long enough that somebody has
                // gone to do something else; tell them it stopped.
                if (pyramidRowName()) {
                    window.PlexoraDesktop?.notifyIfAway({
                        title: "Import failed",
                        body: error.message || "Import failed.",
                    });
                }
                // Whatever already landed stays landed -- those projects
                // exist -- so the message names how far it got.
                render("proposal");
                setStatus(results.length
                    ? `${error.message || "Import failed."} ${results.length} `
                      + `of ${targets.length} were imported.`
                    : (error.message || "Import failed."), true);
                return;
            }
            results.push(result);
            resultAt.push(at);
        }
        const built = pyramidRowName();
        if (built) {
            window.PlexoraDesktop?.notifyIfAway({
                title: `${built} is ready`,
                body: "The pyramidized copy is built and the sample is opening.",
            });
        }
        finishSamples(results, resultAt);
    }

    async function submitScoped() {
        render("importing");
        setStatus("Adding…");
        try {
            const response = await fetch(plexoraUrl("import/layers"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({sample: state.sample, paths: state.picks,
                                      answers: state.answers}),
            });
            const result = await readJson(response);
            if (!response.ok) throw new Error(result.error || "Import failed.");
            return finishScoped(result);
        } catch (error) {
            render("proposal");
            setStatus(error.message || "Import failed.", true);
        }
    }

    /**
     * What happens once the records exist.
     *
     * One sample opens itself, which is the whole point of the dialog: the
     * record is there, and whatever is still building keeps building behind
     * the viewer, which already knows how to wait. SEVERAL do not -- opening
     * one of six is a choice nobody made, and it would take the other five's
     * progress off the screen -- so the rails stay up and the primary offers
     * the first by name.
     */
    function finishSamples(results, at) {
        state.results = results;
        state.registered = results.map((result) => result.name);
        render("importing");
        // What the POST itself finished, painted as finished: the re-render
        // above starts every row at "waiting", and `watch` only reports the
        // builds that carry on after it -- so an image that took minutes to
        // pyramidize would otherwise flip back to "waiting" as it completes.
        results.forEach((result, index) => {
            const sampleAt = at ? at[index] : index;
            (result.layers || []).forEach((entry) => {
                if (entry.status !== "ready") return;
                paintLine(state.bars?.get(`${sampleAt}:${entry.id}`), {status: "ready"});
            });
        });
        const pending = results.some((result) => result.pending);
        const go = part("go");
        go.disabled = false;
        if (results.length === 1) {
            setStatus(pending
                ? "Registered. Opening — the rest keeps building behind the viewer."
                : "Registered. Opening…");
            go.textContent = "Open sample";
            go.onclick = () => openResult(results[0]);
            // The record exists, so the sample is openable now.
            window.setTimeout(() => {
                if (!state || state.results !== results) return;
                go.onclick();
            }, OPEN_AFTER_MS);
            return;
        }
        setStatus(pending
            ? `${results.length} samples registered — what is still building `
              + "carries on without this window."
            : `${results.length} samples registered.`);
        go.textContent = `Open ${results[0].name}`;
        go.onclick = () => openResult(results[0]);
    }

    function openResult(result) {
        const target = result.redirect || `/${encodeURIComponent(result.name)}`;
        close();
        PlexoraRouter.go(target);
    }

    async function finishScoped(result) {
        setStatus("Added.");
        if (result.reload) {
            // A mask inserts the "Area" placeholder into imageData, and every
            // channel index on this page is wired into the GL pass. Said out
            // loud rather than reloading under the user's feet.
            setStatus("Added. Reloading, because a mask changes the channel list…");
            window.setTimeout(() => window.location.reload(), 600);
            return;
        }
        try {
            await window.__plexora?.adoptLayers?.();
        } catch (error) {
            console.error("importSample: could not adopt the new layers.", error);
        }
        if (result.table) {
            // A cell table taken on in place: the page re-reads the read spec
            // and the columns, which is what offers Centroids and the cell
            // tools -- now, or when a large one has been read behind the
            // viewer, with the navbar chip saying how far it has got.
            try {
                await window.PlexoraLoadProgress?.track?.(state.sample);
            } catch (error) {
                console.error("importSample: could not take on the table.", error);
            }
        }
        const after = state && state.onAdded;
        close();
        try {
            after?.(result);
        } catch (error) {
            console.error("importSample: the caller could not repaint.", error);
        }
    }

    /**
     * The same rows, as the progress rail the rest of the app already uses.
     *
     * `.connect-steps` is what the connection wizard and the job panel draw a
     * multi-step wait with -- a mark per step that fills as it lands. Reused
     * rather than restated, because a user who has watched a node connect has
     * already learnt to read it, and because "waiting…" against eight
     * identical rows says nothing about which of them is moving.
     */
    function renderImporting() {
        const body = part("body");
        body.innerHTML = "";
        const samples = state.proposal?.samples || [];
        state.bars = new Map();
        samples.forEach((sample, at) => {
            // A heading per sample once there is more than one, or six rails
            // run together into one list and nothing says which slide a
            // failing mask belongs to.
            if (samples.length > 1) {
                body.appendChild(el("div", "plx-import-steps-head",
                                    state.registered?.[at] || sample.name));
            }
            const steps = el("ul", "connect-steps plx-import-steps");
            (sample.layers || []).forEach((layer) => {
                // A note is recorded, not prepared: a rail row for it would
                // say "waiting" for something that never starts.
                if (layer.role === "note") return;
                const step = el("li", "connect-step");
                step.appendChild(el("span", "connect-step-mark"));
                step.appendChild(el("span", "connect-step-label",
                                    layer.label || layer.id));
                const stage = el("span", "plx-import-step-stage", "waiting");
                step.appendChild(stage);
                const bar = el("span", "plx-import-step-bar");
                const fill = el("span", "plx-import-step-fill");
                bar.appendChild(fill);
                bar.hidden = true;
                step.appendChild(bar);
                let substeps = null;
                if (buildsPyramid(sample, layer)) {
                    const rail = el("ol", "connect-steps plx-import-substeps");
                    substeps = {rail, items: new Map()};
                    PYRAMID_STEPS.forEach(([key, label]) => {
                        const item = el("li", "connect-step");
                        item.dataset.stage = key;
                        item.appendChild(el("span", "connect-step-mark"));
                        item.appendChild(el("span", "connect-step-label", label));
                        rail.appendChild(item);
                        substeps.items.set(key, item);
                    });
                    step.appendChild(rail);
                }
                steps.appendChild(step);
                // Keyed by SAMPLE and layer: two slides in one import both
                // hold a layer called `image`, and one map would have the
                // second sample's progress paint over the first's.
                const line = {step, stage, bar, fill, substeps};
                state.bars.set(`${at}:${layer.id}`, line);
                // The status document names the mask by its job, not by the
                // proposal row it came from.
                if (layer.role === "mask") state.bars.set(`${at}:__mask__`, line);
            });
            body.appendChild(steps);
        });
        part("add").hidden = true;
        part("go").disabled = true;
        setReason("");
        (state.registered || []).forEach((name, at) => watch(name, at));
    }

    function watch(name, at) {
        state.watching = state.watching || new Set();
        if (state.watching.has(name)) return;
        state.watching.add(name);
        const live = () => Boolean(state) && (state.registered || []).includes(name);
        let wasPending = false;
        const tick = async () => {
            if (!live()) return;
            let document_ = null;
            try {
                const response = await fetch(
                    `${plexoraUrl("import/status")}?sample=${encodeURIComponent(name)}`);
                document_ = await response.json();
            } catch (error) { /* one missed tick */ }
            if (!live()) return;
            paintSteps(at, document_);
            if (document_?.pending) {
                wasPending = true;
                window.setTimeout(tick, POLL_MS);
            } else if (wasPending && document_) {
                window.PlexoraDesktop?.notifyIfAway({
                    title: `${name} is ready`,
                    body: "Everything in the sample has finished preparing.",
                });
            }
        };
        tick();
    }

    /**
     * The rail while `/import/sample` is still running, read by its token.
     *
     * Polled faster than `watch`, because what it shows is the wait somebody
     * is staring at. Returns the stop function the POST calls when it
     * answers; `watch` takes over from there, by the sample's name.
     */
    function watchRegistration(token, at) {
        let stopped = false;
        const tick = async () => {
            if (stopped || !state) return;
            let document_ = null;
            try {
                const response = await fetch(
                    `${plexoraUrl("import/status")}?token=${encodeURIComponent(token)}`);
                document_ = await response.json();
            } catch (error) { /* one missed tick */ }
            if (stopped || !state) return;
            if (document_?.registering) paintSteps(at, document_);
            window.setTimeout(tick, REGISTER_POLL_MS);
        };
        window.setTimeout(tick, REGISTER_POLL_MS);
        return () => { stopped = true; };
    }

    function paintSteps(at, document_) {
        Object.entries(document_?.layers || {}).forEach(([id, entry]) => {
            paintLine(state.bars?.get(`${at}:${id}`), entry);
        });
    }

    /** One rail row, from one entry of a status document. */
    function paintLine(line, entry) {
        if (!line || !entry) return;
        // The job document has three statuses and `pending` covers
        // two different things: a build that is running, and a layer
        // nothing has started on -- a modality whose plugin is not
        // installed, or a build that starts once the sample exists, which
        // park at `stage: "waiting"` with a message saying why. Only the
        // first of those is the moving mark; the second is not progress
        // and must not pulse.
        const done = entry.status === "ready";
        const failed = entry.status === "failed";
        const waiting = !done && !failed
            && (entry.stage === "waiting" || !entry.stage);
        const active = !done && !failed && !waiting;
        line.step.classList.toggle("is-done", done);
        line.step.classList.toggle("is-failed", failed);
        line.step.classList.toggle("is-active", active);
        const percent = Math.max(0, Math.min(100, Number(entry.progress) || 0));
        // A conversion's own sub-step already carries its percent in the
        // label ("Building pyramid levels, level 2 of 6 (41%)").
        line.stage.textContent = done ? "ready"
            : failed ? (entry.error || "failed")
            : waiting ? (entry.message || "waiting")
            : entry.substage ? (entry.stage_label || "Preparing…")
            : `${entry.stage_label || "preparing"}…`
              + (percent ? ` ${percent}%` : "");
        // A bar only where there is a number to show: a step that has
        // not reported one keeps the pulsing mark and nothing else --
        // except a pyramid build, whose bar slides from the first moment
        // so a long open never looks like nothing is happening.
        const indeterminate = Boolean(line.substeps) && active && percent === 0;
        if (line.bar) {
            line.bar.hidden = !(active && (percent > 0 || indeterminate));
            line.bar.classList.toggle("is-indeterminate", indeterminate);
            line.fill.style.width = indeterminate ? "" : `${percent}%`;
        }
        if (line.substeps) paintSubsteps(line.substeps, entry, {done, failed, active});
    }

    /** Light a pyramid build's sub-rail up to the stage it reports. */
    function paintSubsteps(substeps, entry, {done, failed, active}) {
        // Folded once the row is ready: the one-line "ready" says it all.
        substeps.rail.hidden = done;
        const keys = PYRAMID_STEPS.map(([key]) => key);
        const current = keys.indexOf(entry.substage);
        keys.forEach((key, index) => {
            const item = substeps.items.get(key);
            const reached = current >= 0 && index <= current;
            item.classList.toggle("is-done", done || (reached && index < current));
            item.classList.toggle("is-active", active && index === current);
            item.classList.toggle("is-failed", failed && index === current);
        });
    }

    // -- rendering ----------------------------------------------------------

    function render(phase) {
        if (!state) return;
        // Every phase rebuilds the body, and an orb whose canvas is gone
        // would go on drawing it every frame.
        stopReadingOrb();
        state.phase = phase;
        dialog.dataset.phase = phase;
        // The chip it opened from is about to be moved or taken off screen,
        // and so is the Browse a card's menu hangs from.
        closeMenu(false);
        closeBrowseMenu(false);
        // The machine chip lives in the pick control, so it is on screen in
        // `pick` and while a card's inline picker is open -- which browses
        // exactly as the pick state does -- and nowhere else.
        if (state.adding && phase === "proposal") dialog.dataset.adding = "1";
        else delete dialog.dataset.adding;
        if (phase === "pick") renderPick();
        else if (phase === "proposal") renderProposal();
        else renderImporting();
        paintSubtitle();
        // The body was just rebuilt, so whatever had the keyboard is gone;
        // a render that knows where it should go now says so.
        const target = state.focusTarget;
        state.focusTarget = null;
        if (target && target.isConnected) target.focus();
    }

    /**
     * The line under the title, which is the dialog's whole voice.
     *
     * In `pick` it says what to do; in `proposal` it says what pressing Import
     * would create; in `importing` it says what is happening. One line, and it
     * is why nothing inside the body has to repeat any of it.
     */
    function paintSubtitle() {
        const line = part("subtitle");
        if (!line) return;
        line.classList.toggle("is-reading", state.phase === "proposal" && !state.proposal);
        if (state.phase === "proposal" && !state.proposal) {
            const picks = state.picks || [];
            line.textContent = "";
            line.appendChild(readingOrb());
            line.appendChild(el("span", null,
                `Reading ${basename(picks[0]) || "the files"}`
                + `${picks.length > 1 ? ` and ${picks.length - 1} more` : ""}…`));
            return;
        }
        if (state.phase === "pick") {
            // The modality path is always scoped -- it is the requirements
            // modal asking for the one thing a tool needs -- so it names both
            // what to pick and which sample it joins.
            const kind = state.modality
                ? state.modality.replace(/_/g, " ") : null;
            line.textContent = kind && scoped()
                ? `Pick the ${kind} data for ${state.sample}.`
                : kind
                    ? `Pick the ${kind} data.`
                    : scoped() && state.intent
                        ? "A file or a folder — an OME-Zarr store, a run's "
                          + "output. Plexora reads what it is."
                        : scoped()
                            ? `Pick the files to add as layers of ${state.sample}.`
                            : "Point Plexora at a file or a folder. It works out "
                              + "what each one is.";
        } else if (state.phase === "proposal") {
            line.textContent = summarize(state.proposal);
        } else {
            const names = state.registered || [];
            const building = !names.length && !scoped() && pyramidRowName();
            line.textContent = building
                ? `Building a pyramidized copy of ${building} — this happens once.`
                : names.length > 1
                ? `${names.length} samples registered.`
                : names.length
                    ? `Opening ${names[0]}…`
                    : scoped() ? `Adding to ${state.sample}…` : "Registering…";
        }
    }

    /** The first row this import pyramidizes, by name, or null. */
    function pyramidRowName() {
        for (const sample of state.proposal?.samples || []) {
            const layer = (sample.layers || []).find((row) => buildsPyramid(sample, row));
            if (layer) return layer.label || basename(layer.src);
        }
        return null;
    }

    function basename(path) {
        return String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop();
    }

    // -- the buttons that open it ------------------------------------------

    /**
     * Every "Import Sample..." on every page, bound in one place.
     *
     * By id rather than by each page's own controller, because the same
     * dialog is opened from four surfaces that otherwise share nothing: the
     * library header, its empty state, the home page's footer and the navbar
     * menu. A controller per surface would be four copies of one line.
     *
     * The home page's is `sample-import-home`, and it is a FOOTER link rather
     * than that page's primary action: the landing page opens one pick in one
     * gesture through `/import/sample` itself (views/quickViewLanding.js), and
     * this is where somebody goes when the pick needs a decision first -- several
     * samples in one folder, a name, a dataset, a question answered.
     *
     * `PlexoraPage.register` re-runs on every routed navigation, and binding is
     * idempotent (`dataset.bound`), so a page visited twice does not open two
     * dialogs per click.
     */
    if (typeof PlexoraPage !== "undefined") {
        PlexoraPage.register(function () {
            ["sample-import", "sample-import-empty", "sample-import-home",
             "sample-import-menu"].forEach((id) => {
                const button = document.getElementById(id);
                if (!button || button.dataset.importBound) return;
                button.dataset.importBound = "1";
                button.addEventListener("click", (event) => {
                    event.preventDefault();
                    open({
                        // The library page re-lists when the dialog closes, so
                        // a sample imported into the folder currently being
                        // looked at appears without a reload.
                        onClose: () => window.PlexoraOpenProject?.refresh?.(),
                    });
                });
            });
        });
    }

    return {open, close, dropPaths};
})();
