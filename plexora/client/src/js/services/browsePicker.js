/**
 * browsePicker.js -- what every Browse button opens.
 *
 * **The file explorer first, on every machine** (services/explorer/): a
 * floating window over the page that lists this server's filesystem or a
 * node's, with drives, pins, search and the selection rules the field asked
 * for. It is the same window in a browser tab, in the desktop app and on a
 * cluster, which is the point -- the Recent list, the pins and the window's
 * size are the explorer's, and a Browse that opened Finder on one machine and
 * a listing on another kept none of them.
 *
 * **The machine's own dialog is one step away**: the explorer's "System file
 * browser" button beside its title, wherever one exists. The explorer closes
 * as that dialog opens, and what is chosen there is the answer. POST /browse_path (server/utils/native_dialog.py) opens it on
 * the machine the server runs on, or on a node's; the desktop app opens its
 * own. /browse_capability says which of three answers a machine gives:
 *
 *   "hybrid"    one panel takes a file OR a folder -- macOS.
 *   "kinds"     a desktop, but one kind at a time -- Windows, Linux.
 *   "list"      no desktop at all -- a compute node, a container, notebook
 *               mode; the overflow offers nothing.
 *
 * `chooseKind` asks "file or folder?" only where it must: a "kinds" machine
 * asked for either from the overflow, and a page that loads this file
 * without the explorer -- no page in the app does -- so a Browse there is
 * never a dead end.
 */

/**
 * @function chooseKind - which single-kind native dialog to open, on a machine
 * that has no dialog taking both.
 *
 * Drawn as ONE control with two halves rather than a list of two commands:
 *
 *     +----------------------+----------------------+
 *     |  📄  File            |  📁  Folder          |
 *     |  .ome.tiff · .svs    |  .ome.zarr · dicom   |
 *     +----------------------+----------------------+
 *
 * The example lines are the point. "File or folder?" is not a question about
 * what the user wants, it is a question about what their FORMAT is -- an
 * OME-Zarr is a directory and an OME-TIFF is a file, and nobody should have to
 * remember which. `examples` picks the pair, so the Data field never claims to
 * want a .svs and the mask field never offers a .svs mask.
 *
 * Anchored under the Browse button rather than drawn as a modal, because it is
 * a step INSIDE pressing that button and not a place the user has arrived at.
 * Resolves "file", "directory", or null -- and null is a dismissal, which is
 * the same answer as cancelling the dialog it would have opened, so callers
 * treat the two identically.
 *
 * The road not taken: one Browse takes either kind (see the file header),
 * so this is reached only from the explorer's "System file browser" on a
 * single-kind desktop, or on a page that has no explorer.
 *
 * Nothing is cached between clicks. The capability is a fact about the machine
 * on the other end of `node`, which the Local/Remote switch can change under a
 * button that is already mounted -- and this module's standing rule is that the
 * answer to "can you do this?" is the attempt, not a remembered verdict.
 */
//: What each half says underneath its label. The question "file or folder?" is
//: only answerable if you know which of your things is which, and that is a
//: fact about the FORMAT rather than about you -- so the control answers it
//: instead of asking -- ".ome.tiff · .svs" under the Data field would be a
//: confident lie.
//:
//: Keyed by a name of its own rather than by `filter`, because the image and
//: the mask fields share filter "image" and must NOT share these: a mask is a
//: label image, and nobody has one in .svs. A button says which set it wants
//: with `data-browse-examples`, defaulting to the filter's own name; a name
//: with no entry gets no example line rather than a wrong one.
//:
//: Two formats each, and the ones people actually have. This IS the list of
//: accepted formats now -- the sentence that used to repeat it under the field
//: said the same thing twice and agreed with itself only for as long as
//: somebody remembered to edit both.
const KIND_EXAMPLES = {
    image: { file: ".ome.tiff · .svs", directory: ".ome.zarr · DICOM" },
    mask: { file: ".ome.tiff · .png", directory: ".ome.zarr · SpatialData" },
    data: { file: ".csv · .parquet · .h5ad", directory: "SpatialData (.zarr)" },
    // Import Sample, where the halves are not asking which ROLE the file plays
    // -- detection answers that -- only which shape of thing to browse for.
    // The folder half leads, because a run folder is the case this flow
    // exists for and picking one is one gesture instead of six.
    sample: { file: "image · mask · table · transcripts",
              directory: "Xenium · SpatialData · Visium · .zarr" },
};

//: The examples above that name the spatial-transcriptomics modality. A
//: licence without `transcriptomics` does not detect such a run at import
//: (import_proposal._licensed), so it is not offered here either: the words
//: are left out -- not greyed, not explained.
const TRANSCRIPTOMICS_EXAMPLES = new Set(["Xenium", "Visium", "transcripts"]);

/** A KIND_EXAMPLES entry as this licence offers it. Asked when the control is
 *  built, so a licence adopted in Settings shows the words on the next one. */
function offeredExamples(entry) {
    if (window.PlexoraPro?.allows?.("transcriptomics")) return entry;
    const keep = (text) => String(text || "").split(" · ")
        .filter((word) => word && !TRANSCRIPTOMICS_EXAMPLES.has(word)).join(" · ");
    return { file: keep(entry.file), directory: keep(entry.directory) };
}

/**
 * @function buildSplitControl - the two-half control itself, without deciding
 * where it lives. Used two ways: as the Browse button on a form field, and as
 * a floating menu for the callers that still have to ask after the fact. (The
 * home page and the import dialog ask with File | Folder tabs of their own.)
 * @param examples - which KIND_EXAMPLES entry labels the halves. A name of
 *   its own rather than the filter, because image and mask share a filter and
 *   want different examples.
 * @param choose - called with "file" or "directory".
 * @param labels - optional `{file, directory}` overriding the words on the two
 *   halves. Beside a path box the control is one of four things in a row and
 *   "File" / "Folder" is all the room there is; standing alone as a page's
 *   primary action it is a button rather than a qualifier, and a button is
 *   named for what pressing it does. The KINDS do not change, only the
 *   wording -- which is why this is an argument and not a second component.
 */
function buildSplitControl(examples, choose, labels = null) {
    const control = document.createElement("div");
    control.className = "browse-kind-split";
    control.setAttribute("role", "group");
    control.setAttribute("aria-label", "Browse for a file or a folder");

    const shown = offeredExamples(KIND_EXAMPLES[examples] || {});
    [["file", labels?.file || "File", "fa-file"],
     ["directory", labels?.directory || "Folder", "fa-folder"]].forEach(([kind, label, glyph]) => {
        const half = document.createElement("button");
        half.type = "button";
        half.className = `browse-kind-half is-${kind}`;

        // Font Awesome, not the emoji this used to draw through CSS `content`.
        // Emoji render in their own colours whatever the surface, which put two
        // full-saturation glyphs in a UI whose every other icon is a muted
        // outline -- see #topBar .view-menu-icon, which this is styled after.
        const icon = document.createElement("span");
        icon.className = `fas ${glyph} browse-kind-icon`;
        icon.setAttribute("aria-hidden", "true");
        half.appendChild(icon);

        // Label over example, beside the icon rather than under it.
        const text = document.createElement("span");
        text.className = "browse-kind-text";
        half.appendChild(text);

        const name = document.createElement("span");
        name.className = "browse-kind-name";
        name.textContent = label;
        text.appendChild(name);

        const example = shown[kind];
        if (example) {
            const hint = document.createElement("span");
            hint.className = "browse-kind-example";
            hint.textContent = example;
            // Read out, but not as the middle dot -- which a screen reader
            // announces as "middle dot" between every format. The visible line
            // stays the separator it should be; the spoken one is a list.
            hint.setAttribute("aria-hidden", "true");
            half.setAttribute(
                "aria-label", `${label}: ${example.replace(/ · /g, ", ")}`);
            text.appendChild(hint);
        }

        half.addEventListener("click", () => choose(kind));
        control.appendChild(half);
    });
    return control;
}

/**
 * Arrow-key movement between the two halves. `role="menu"` promises it, and
 * Tab alone does not keep the promise: from the last half Tab leaves for
 * whatever is behind the control.
 */
function stepBetweenHalves(control, event) {
    const back = ["ArrowLeft", "ArrowUp"].includes(event.key);
    if (!back && !["ArrowRight", "ArrowDown"].includes(event.key)) return false;
    event.preventDefault();
    const halves = [...control.querySelectorAll(".browse-kind-half")];
    const at = halves.indexOf(document.activeElement);
    halves[(at + (back ? -1 : 1) + halves.length) % halves.length]?.focus();
    return true;
}

//: What this machine (or the node a field points at) can put on screen, kept
//: per machine for the life of the page. Memoized because every Browse control
//: on a form asks the same question of the same machine as it mounts, and the
//: answer cannot change under them -- a computer does not grow a desktop while
//: a form is open. A failure is NOT kept: caching a network blip would leave
//: the field with the substitute picker for the rest of the session.
const capabilityCache = new Map();

function browseCapability(node) {
    // The desktop app's own dialogs, for this machine: one kind at a time on
    // every platform, so the File/Folder control. A node's machine is asked,
    // as ever -- its dialog opens on ITS screen.
    if (!node && window.PlexoraDesktop) return Promise.resolve("kinds");
    const key = node || "";
    if (!capabilityCache.has(key)) {
        capabilityCache.set(key, fetch(plexoraUrl("browse_capability"), {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({node: node}),
        }).then((response) => response.json())
          .then((result) => result.dialogs || "none")
          .catch((error) => { capabilityCache.delete(key); throw error; }));
    }
    return capabilityCache.get(key);
}

/**
 * Where to hang the menu. Not simply `anchorEl.getBoundingClientRect()`,
 * because the element that asked for this may be the very one that has already
 * been REPLACED by a File/Folder control: `applyCapability` hides the button
 * rather than removing it, and getBoundingClientRect() on a hidden element is
 * four zeros. That pinned the menu to the top-left corner of the window --
 * open, correctly drawn, and a whole page away from what was clicked, which is
 * exactly how a menu goes unnoticed.
 *
 * The control that replaced it is the thing actually on screen, so measure
 * that instead; failing that, the anchor's parent, which at least contains it.
 */
function anchorBox(anchorEl) {
    const box = anchorEl.getBoundingClientRect();
    if (box.width || box.height) return box;
    const shown = anchorEl.parentElement?.querySelector(".browse-kind-split");
    return (shown || anchorEl.parentElement || document.body)
        .getBoundingClientRect();
}

function chooseKind(anchorEl, examples) {
    return new Promise((resolve) => {
        let menu;

        let done = false;
        // One exit for every way out -- a chosen kind, Esc, a click elsewhere,
        // a scroll that would leave the menu floating away from its button.
        // Without a single close, dismissing the menu one way left the
        // listeners of the others attached to the document for the life of the
        // page.
        function close(answer) {
            if (done) return;
            done = true;
            document.removeEventListener("keydown", onKey, true);
            document.removeEventListener("pointerdown", onOutside, true);
            window.removeEventListener("resize", onScroll, true);
            window.removeEventListener("scroll", onScroll, true);
            menu.remove();
            // Back to the button that opened this. A menu that closes into
            // nowhere strands keyboard focus at the top of the document.
            if (answer === null && anchorEl.focus) anchorEl.focus();
            resolve(answer);
        }

        function onKey(event) {
            if (event.key === "Escape") {
                // Captured at the document, so this runs before the <dialog>
                // this may be sitting inside gets to act on it: preventDefault
                // is what stops Esc closing the whole modal out from under a
                // menu the user only meant to dismiss.
                event.preventDefault();
                event.stopPropagation();
                close(null);
                return;
            }
            stepBetweenHalves(menu, event);
        }
        function onOutside(event) {
            if (!menu.contains(event.target)) close(null);
        }
        function onScroll() { close(null); }

        menu = buildSplitControl(examples, close);
        menu.classList.add("is-floating");

        // Positioned against the viewport, so the control is not clipped by a
        // form field's `overflow` the way an absolutely-positioned child would
        // be -- the path row is inside a scrolling card on the import page,
        // and `position: fixed` escapes an ancestor's overflow entirely.
        //
        // No `minWidth` from the button: this is a sized component with two
        // equal halves, and stretching it to the width of whatever opened it
        // would make the same control a different shape on every field.
        const box = anchorBox(anchorEl);
        menu.style.top = `${box.bottom + 4}px`;
        menu.style.left = `${box.left}px`;

        // Into the modal, when the button that opened this is inside one.
        // requirementsModal.js and channelNamesUpload.js both carry a Browse
        // button and both open with showModal(), which puts the dialog in the
        // browser's TOP LAYER -- above every z-index on the page, including
        // one set to clear it. A menu appended to <body> would be painted
        // behind the modal it belongs to: present, correct, and invisible.
        // Neither dialog sets transform/filter/contain, so `fixed` still
        // resolves against the viewport in here.
        const host = anchorEl.closest?.("dialog[open]") || document.body;
        host.appendChild(menu);

        // Flip above the button when there is no room below it.
        const spread = menu.getBoundingClientRect();
        if (spread.bottom > window.innerHeight && box.top > spread.height) {
            menu.style.top = `${box.top - spread.height - 4}px`;
        }
        if (spread.right > window.innerWidth) {
            menu.style.left = `${Math.max(4, window.innerWidth - spread.width - 4)}px`;
        }

        menu.querySelector(".browse-kind-half")?.focus();
        document.addEventListener("keydown", onKey, true);
        document.addEventListener("pointerdown", onOutside, true);
        window.addEventListener("resize", onScroll, true);
        window.addEventListener("scroll", onScroll, true);
    });
}

/**
 * @function browseForPath - opens the file explorer and calls back with what
 * was chosen.
 * @param mode - "any" (a file OR a folder, which is what every path field
 *   wants: an image is an .ome.tif or a .zarr *directory*, and which one it is
 *   is a fact about the format rather than a question to put to the user),
 *   "file", or "directory". Read by the explorer as its selection rule.
 * @param filter - one of native_dialog.py's FILTERS keys ("image", "csv",
 *   "h5ad", "data", "channels", "any"). The explorer greys out the files the
 *   field cannot take; the system dialog narrows its file-type dropdown.
 * @param node - whose filesystem: absent for the machine running the server,
 *   a node's name for that machine. Passed through to the explorer and to the
 *   system dialog alike -- a listing of the wrong machine would be worse than
 *   none.
 * @param start - where to open: whatever the field already holds, so
 *   re-picking a file starts in the folder it came from. The server turns a
 *   file's path into the folder holding it.
 * @param anchorEl - the button this was launched from; the "file or folder?"
 *   chooser hangs under it on a page with no explorer.
 * @param examples - which KIND_EXAMPLES entry labels that chooser.
 * @param accept, select, primary, title - passed to the explorer as they are
 *   (PlexoraFileExplorer.pick).
 * @param onPicked - called with the chosen path (an array of them when
 *   `select` is "many"); not called on a cancel.
 * @param onUnavailable - called with the error if nothing could be shown at
 *   all.
 */
async function browseForPath({mode = "file", filter = "any", node = null,
                              start = "", anchorEl = null, examples = null,
                              onPicked, onUnavailable,
                              accept = null, select = null, primary = "",
                              title = ""} = {}) {
    try {
        const explorer = window.PlexoraFileExplorer;
        if (explorer) {
            const picked = await explorer.pick({
                mode, filter, node: node || "", start,
                accept, select, primary, title,
                systemDialog: systemDialogFor({node, filter, start, anchorEl, examples}),
            });
            if (picked && onPicked) onPicked(picked);
            return;
        }
        // A page without the explorer: the machine's own dialog, asking which
        // kind first where its dialogs take one at a time.
        let kind = mode;
        if (kind === "any" && await browseCapability(node) !== "hybrid") {
            kind = anchorEl ? await chooseKind(anchorEl, examples || filter) : "file";
            // Dismissed. The same answer as cancelling the dialog this would
            // have opened, and it gets the same silence -- an onUnavailable
            // here would report a failure at somebody who changed their mind.
            if (!kind) return;
        }
        const picked = await openSystemDialog({kind, filter, node, start});
        if (picked && onPicked) onPicked(picked);
    } catch (error) {
        if (onUnavailable) onUnavailable(error instanceof Error ? error : new Error(String(error)));
    }
}

/**
 * The hook behind the explorer's "System file browser" button: the machine's
 * own file dialog, offered only where `available()` says one exists, and
 * named in its tooltip for what it is.
 */
function systemDialogFor({node = null, filter = "any", start = "", anchorEl = null,
                          examples = null} = {}) {
    const desktop = !node && Boolean(window.PlexoraDesktop);
    const platform = desktop ? window.PlexoraDesktop.platform : "";
    let capability = null;
    return {
        //: What the machine's dialog is called, for the explorer's tooltip:
        //: "Finder", "File Explorer", or "" where it has no common name.
        get app() {
            if (platform === "windows") return "File Explorer";
            if (platform === "macos" || capability === "hybrid") return "Finder";
            return "";
        },
        async available() {
            // The desktop app's own dialogs take one kind at a time; the Mac
            // under it has a panel that takes either, through the server.
            capability = desktop
                ? (await serverDialogs() === "hybrid" ? "hybrid" : "kinds")
                : await browseCapability(node);
            return capability;
        },
        // `onChosen` runs once the kind is settled and just before the dialog
        // shows: the explorer closes then, so a dismissed "file or folder?"
        // menu leaves it open.
        async open(kind, {multiple = false, anchor = null, onChosen = null} = {}) {
            if (!kind) {
                kind = await chooseKind(anchor || anchorEl, examples || filter);
                if (!kind) return null;
            }
            if (onChosen) onChosen();
            return openSystemDialog({kind, filter, node, start, multiple});
        },
    };
}

/**
 * One native dialog, of one kind ("file", "directory", or "any" where the
 * machine has a panel for both). Resolves the chosen path, an array when
 * `multiple` and the dialog takes several, or null for a cancel; throws when
 * no dialog could be shown.
 */
async function openSystemDialog({kind, filter = "any", node = null, start = "",
                                 multiple = false}) {
    if (!node && window.PlexoraDesktop) {
        if (kind === "any") {
            if (await serverDialogs() === "hybrid") {
                const response = await fetch(plexoraUrl("browse_path"), {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({mode: "any", filter}),
                });
                const result = await response.json();
                if (!response.ok) throw new Error(result.error || "The file dialog could not be opened.");
                return result.path || null;
            }
            kind = "file";
        }
        const paths = await window.PlexoraDesktop.pickPaths({
            mode: kind, filter, defaultPath: start || null, multiple,
        });
        if (!paths || !paths.length) return null;
        return multiple ? paths : paths[0];
    }
    const response = await fetch(plexoraUrl("browse_path"), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        // `node` says WHICH machine's dialog to open. Absent means the one
        // running the server; a name means that node's, which for the node
        // `plexora connect` starts is the user's own computer -- the only
        // machine in the arrangement that reliably has a screen.
        body: JSON.stringify({mode: kind, filter: filter, node: node}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The file dialog could not be opened.");
    // result.path === null just means the user cancelled the dialog.
    return result.path || null;
}

/**
 * What the server's own machine can put on screen, asked without the desktop
 * shortcut `browseCapability` takes: the desktop app needs to know whether the
 * machine under it is a Mac, whose panel takes a file or a folder in one go.
 */
function serverDialogs() {
    if (!capabilityCache.has("@server")) {
        capabilityCache.set("@server", fetch(plexoraUrl("browse_capability"), {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({}),
        }).then((response) => response.json())
          .then((result) => result.dialogs || "none")
          .catch(() => { capabilityCache.delete("@server"); return "none"; }));
    }
    return capabilityCache.get("@server");
}

// -- a file dropped on a field ------------------------------------------------
//
// In the desktop app a file dragged in from Explorer or Finder arrives as a
// PATH (desktopBridge.js re-dispatches the shell's drag as `plexora:native-drag`
// and a cancelable `plexora:native-drop`). That makes every path box a place a
// file can be dropped, with nothing added to it: the box lights while a file it
// would take is over it, and the drop fills it exactly as a Browse pick does.
// A drop nobody claims still opens the Import dialog, which is what dropping a
// file anywhere else in the window has always done.
//
// A browser tab gets nothing here. An HTML5 drop hands a page the BYTES of a
// file and never where it lives, and a box that wants a path to a 40 GB slide
// has no use for its bytes -- so in a browser a path box is exactly what it was.
//
// One registry and one pair of document listeners for every zone on every
// page. Zones are keyed by element in a WeakMap, so a view that re-renders, or
// a page the router swaps out, leaves nothing to clean up: an element that is
// gone can no longer be under the pointer, and its entry goes with it.

//: zone element -> {filter, mode, accept, count, enabled, accepts, hint, onOver, onDrop}
const DROP_ZONES = new WeakMap();
let dropListening = false;
//: The zone lit right now, and how: {zone, state: "over" | "refused"}.
let dropLit = null;
//: What the drag in progress carries, for a bridge that names it only once.
let dropPaths = [];
//: Where the pointer last was during this drag, in CSS pixels.
let dropPoint = null;
//: `(paths) => Promise<plan | null>`: what the files ARE relative to the open
//: project (dropRouter.js asks the server). Asked once per drag.
let planProvider = null;
//: The answer for the drag in progress: {key, plan, pending}.
let dropPlan = null;
//: Every file suffix the explorer knows, stores aside, built on first use.
let knownSuffixes = null;

/**
 * Put a path in a box the way every Browse does: the value, then `input` and
 * `keyup` for the live validation fields already run, and a bubbling `change`
 * -- the one the Local/Remote switch shares a file on, since sharing is not
 * something to do per keystroke. A programmatic assignment fires none of them.
 */
function fillPathInput(inputEl, path) {
    if (!inputEl) return;
    inputEl.value = path;
    if (typeof inputEl.scrollWidth === "number") inputEl.scrollLeft = inputEl.scrollWidth;
    inputEl.dispatchEvent(new Event("input"));
    inputEl.dispatchEvent(new Event("keyup"));
    inputEl.dispatchEvent(new Event("change", { bubbles: true }));
}

/**
 * @function provideDropPlan - say what dragged files are, for zones that label
 * themselves by it. `fn(paths)` returns a promise of the plan
 * (`/drop/inspect`) or null. Asked once per drag, on the first move that names
 * the paths; the zone under the pointer is relit when the answer lands.
 */
function provideDropPlan(fn) {
    planProvider = typeof fn === "function" ? fn : null;
}

/** The plan for the drag in progress, asked for if it has not been. */
function planFor(paths) {
    if (!planProvider || !paths.length) return null;
    const key = paths.join("\n");
    if (dropPlan && dropPlan.key === key) return dropPlan;
    const entry = {key, plan: null, pending: true, promise: null};
    dropPlan = entry;
    let asked;
    try {
        asked = Promise.resolve(planProvider(paths.slice()));
    } catch (error) {
        asked = Promise.resolve(null);
    }
    entry.promise = asked.then((plan) => {
        entry.plan = plan || null;
        entry.pending = false;
        // Relit with what it now knows, if this drag is still going on.
        if (dropPlan === entry && dropPoint && dropPaths.length) relight();
        return entry.plan;
    }, () => {
        entry.pending = false;
        return null;
    });
    return entry;
}

/** What the drag in progress knows so far: the plan, or null while asked. */
function currentPlan() {
    return dropPlan && !dropPlan.pending ? dropPlan.plan : null;
}

/**
 * @function attachDropTarget - make an element a place a dropped file goes.
 * Desktop app only; anywhere else it does nothing and says so by returning a
 * no-op.
 *
 * @param zoneEl the element the pointer has to be over: a field's row, a card,
 *   a whole page. The innermost zone under the pointer wins.
 * @param filter / mode / accept what the box takes, in the explorer's own
 *   terms (`PlexoraExplorerTypes.normalizeAccept`): a file whose suffix is one
 *   the explorer knows and the box does not take is refused with the reason,
 *   and a folder box refuses a file. A name the explorer cannot place -- a
 *   Xenium run folder, a suffix it has never heard of -- is the box's own
 *   validation's business, and goes in.
 * @param count "one" (one path; a drag of several lights nothing and falls
 *   through to the Import dialog, which takes several) or "many".
 * @param several for a "one" zone that is the whole of a modal, the reason a
 *   drag of several is refused there rather than passed on (nothing outside a
 *   modal can take it).
 * @param enabled () => boolean; false and the zone is not there, for a field
 *   that is busy reading what it was last given.
 * @param accepts (path, name) => true | string | false, a veto of the
 *   caller's own; a string is the reason given, and false means "not mine":
 *   the zone passes and the drop goes to the zone around it.
 * @param hint (paths, {state, reason, plan}) => string | {label, tone} | "",
 *   the label drawn beside the pointer while the drag is over this zone
 *   (tone "ok", "ask" or "refused"). Not drawn for a zone inside a modal,
 *   which lights itself.
 * @param onOver (lit, state, info) for a zone that says more than its class
 *   does; `info` is {paths, reason, plan}.
 * @param onDrop (path, {zone, x, y, plan}), or the array of paths for "many".
 *   `plan` is the server's plan when one was asked for this drag, else null.
 * @returns a function that unregisters the zone.
 */
function attachDropTarget(zoneEl, {filter = "any", mode = "file", accept = null,
                                   count = "one", several = "", enabled = null,
                                   accepts = null, hint = null, onOver = null,
                                   onDrop = null} = {}) {
    if (!zoneEl || typeof onDrop !== "function" || !window.PlexoraDesktop) {
        return () => {};
    }
    DROP_ZONES.set(zoneEl, {filter, mode, accept, count, several, enabled, accepts,
                            hint, onOver, onDrop});
    zoneEl.setAttribute("data-plx-drop", "");
    listenForDrops();
    return () => {
        if (dropLit && dropLit.zone === zoneEl) lightZone(null, null);
        DROP_ZONES.delete(zoneEl);
        zoneEl.removeAttribute("data-plx-drop");
    };
}

function listenForDrops() {
    if (dropListening) return;
    dropListening = true;
    document.addEventListener("plexora:native-drag", onNativeDragOverField);
    document.addEventListener("plexora:native-drop", onNativeDropOnField);
}

/** The last part of a path, a trailing separator ignored. */
function dropName(path) {
    return String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "";
}

function isKnownFileName(lowered) {
    const types = window.PlexoraExplorerTypes;
    if (!types || !types.EXTENSIONS) return false;
    if (!knownSuffixes) {
        const stores = new Set(types.STORE_SUFFIXES || []);
        const all = new Set();
        Object.values(types.EXTENSIONS).forEach((list) => {
            (list || []).forEach((suffix) => { if (!stores.has(suffix)) all.add(suffix); });
        });
        knownSuffixes = Array.from(all);
    }
    return knownSuffixes.some((suffix) => lowered.endsWith(suffix));
}

/** Why this box will not take that name, or "" when it will. */
function suffixRefusal(name, spec) {
    const types = window.PlexoraExplorerTypes;
    const lowered = name.toLowerCase();
    if (!types || typeof types.normalizeAccept !== "function" || !isKnownFileName(lowered)) {
        return "";
    }
    const rule = types.normalizeAccept({accept: spec.accept, mode: spec.mode, filter: spec.filter});
    if (rule.files === null) return "";
    if (Array.isArray(rule.files) && rule.files.some((suffix) => lowered.endsWith(suffix))) {
        return "";
    }
    if (rule.files === false) return "This box takes a folder, not a file.";
    return rule.words ? `This box takes ${rule.words}.` : "This box takes a different kind of file.";
}

/**
 * What a zone does with these paths: "take" them, "refuse" them with a reason
 * (the drop is aimed at this box and wrong for it), or "pass" (the zone is not
 * a target for this drag at all, so it lights nothing and claims nothing).
 * No paths yet -- a shell that names them only at the drop -- is a take,
 * decided again when they arrive.
 */
function dropVerdict(paths, spec) {
    if (spec.enabled && !spec.enabled()) return {state: "pass"};
    if (!paths.length) return {state: "take"};
    if (spec.count !== "many" && paths.length !== 1) {
        return spec.several ? {state: "refuse", reason: spec.several} : {state: "pass"};
    }
    for (const path of paths) {
        const name = dropName(path);
        if (spec.accepts) {
            const said = spec.accepts(path, name);
            if (typeof said === "string" && said) return {state: "refuse", reason: said};
            // Not this zone's kind of file at all: the drop is somebody
            // else's -- a mask over the ROI panel is the viewer's.
            if (said === false) return {state: "pass"};
        }
        const reason = suffixRefusal(name, spec);
        if (reason) return {state: "refuse", reason};
    }
    return {state: "take"};
}

/** The open modal dialog a drop has to land inside, if any. */
function openModal() {
    const open = Array.from(document.querySelectorAll("dialog[open]"));
    const modal = open.filter((dialog) => {
        try {
            return dialog.matches(":modal");
        } catch (error) {
            return true;
        }
    });
    return modal.length ? modal[modal.length - 1] : null;
}

/** The next registered zone at or above `el`, or null. */
function zoneFrom(el) {
    let zone = el && typeof el.closest === "function" ? el.closest("[data-plx-drop]") : null;
    while (zone && !DROP_ZONES.has(zone)) {
        const up = zone.parentElement;
        zone = up && typeof up.closest === "function" ? up.closest("[data-plx-drop]") : null;
    }
    return zone;
}

/** The innermost registered zone under a point, or null. While a modal is
 *  open only a zone inside it counts: the page under the backdrop is not
 *  there to the pointer, and must not be there to a drop either. */
function zoneAt(x, y) {
    const hit = typeof document.elementFromPoint === "function"
        ? document.elementFromPoint(x, y) : null;
    const zone = zoneFrom(hit);
    if (!zone) return null;
    const modal = openModal();
    if (modal && !modal.contains(zone)) return null;
    return zone;
}

/**
 * The zone these paths land in at a point, and its verdict. Starts at the
 * innermost zone and walks OUTWARD past every zone that passes: three files
 * over a one-path row inside a dialog that takes several are the dialog's,
 * not nobody's. A zone that refuses stops the walk -- the drop was aimed at
 * it -- and so does the open modal's edge.
 */
function resolveZone(x, y, paths) {
    const modal = openModal();
    let zone = zoneAt(x, y);
    while (zone) {
        if (modal && !modal.contains(zone)) break;
        const spec = DROP_ZONES.get(zone);
        const verdict = spec ? dropVerdict(paths, spec) : {state: "pass"};
        if (verdict.state !== "pass") return {zone, spec, verdict};
        zone = zoneFrom(zone.parentElement);
    }
    return {zone: null, spec: null, verdict: {state: "pass"}};
}

function tellZone(zone, lit, state, info) {
    const spec = DROP_ZONES.get(zone);
    if (!spec || typeof spec.onOver !== "function") return;
    try {
        spec.onOver(lit, state, info || {});
    } catch (error) {
        console.error("browsePicker: a drop zone's onOver failed.", error);
    }
}

/** The one label that follows the pointer while it is over a zone. */
function hintElement(create) {
    if (typeof document.getElementById !== "function") return null;
    let el = document.getElementById("plx_drop_hint");
    if (!el && create && document.body && typeof document.createElement === "function") {
        el = document.createElement("div");
        el.id = "plx_drop_hint";
        el.className = "plx-drop-hint";
        el.setAttribute("role", "status");
        el.setAttribute("aria-live", "polite");
        document.body.appendChild(el);
    }
    return el;
}

function hideHint() {
    const el = hintElement(false);
    if (el) el.classList.remove("is-shown");
}

/** What a zone says about the drag over it, as {label, tone}, or null. */
function zoneHint(zone, spec, state, info) {
    if (!spec || typeof spec.hint !== "function") {
        return state === "refused" && info.reason ? {label: info.reason, tone: "refused"} : null;
    }
    let said;
    try {
        said = spec.hint(info.paths || [], {state, reason: info.reason, plan: info.plan});
    } catch (error) {
        said = null;
    }
    if (!said) return null;
    if (typeof said === "string") {
        return {label: said, tone: state === "refused" ? "refused" : "ok"};
    }
    return said.label ? {label: said.label, tone: said.tone || "ok"} : null;
}

/** Draw the label beside the pointer: right of it and below, where the drag
 *  ghost is not, and pulled back inside the window at its edges. */
function paintHint(zone, spec, state, info) {
    const modal = openModal();
    const said = modal && modal.contains(zone) ? null : zoneHint(zone, spec, state, info);
    if (!said || !dropPoint) {
        hideHint();
        return;
    }
    const el = hintElement(true);
    if (!el) return;
    el.textContent = said.label;
    el.dataset.tone = said.tone;
    el.classList.add("is-shown");
    const width = el.offsetWidth || 0;
    const height = el.offsetHeight || 0;
    const right = (window.innerWidth || 0) - 8;
    const bottom = (window.innerHeight || 0) - 8;
    let left = dropPoint.x + 18;
    let top = dropPoint.y + 22;
    if (right > 0 && left + width > right) left = Math.max(8, dropPoint.x - 12 - width);
    if (bottom > 0 && top + height > bottom) top = Math.max(8, dropPoint.y - 12 - height);
    el.style.left = `${Math.round(left)}px`;
    el.style.top = `${Math.round(top)}px`;
}

/** Light one zone (or none) and put out the last. `info` is what the zone is
 *  told and labelled with: {paths, reason, plan}. */
function lightZone(zone, state, info) {
    const before = dropLit;
    const same = before && before.zone === zone && before.state === state;
    if (!same) {
        if (before) before.zone.classList.remove("is-drop-over", "is-drop-refused");
        dropLit = zone && state ? {zone, state} : null;
        if (dropLit) zone.classList.add(state === "refused" ? "is-drop-refused" : "is-drop-over");
        document.documentElement.classList.toggle("has-drop-target", Boolean(dropLit));
        if (before && before.zone !== zone) tellZone(before.zone, false, null);
    }
    if (!dropLit) {
        hideHint();
        return;
    }
    const detail = info || {paths: dropPaths.slice(), reason: "", plan: currentPlan()};
    // Told when it lights and again when the plan lands -- not on every move
    // of the pointer, which is sixty times a second.
    if (!same || detail.plan !== dropLit.plan) tellZone(zone, true, state, detail);
    dropLit.plan = detail.plan;
    paintHint(zone, DROP_ZONES.get(zone), state, detail);
}

/** Light whatever is under the last point, for these paths. */
function relight() {
    if (!dropPoint) return;
    const found = resolveZone(dropPoint.x, dropPoint.y, dropPaths);
    const info = {paths: dropPaths.slice(), reason: found.verdict.reason || "",
                  plan: currentPlan()};
    if (found.verdict.state === "take") lightZone(found.zone, "over", info);
    else if (found.verdict.state === "refuse") lightZone(found.zone, "refused", info);
    else lightZone(null, null);
}

function onNativeDragOverField(event) {
    const detail = event.detail || {};
    if (detail.phase === "leave") {
        dropPaths = [];
        dropPoint = null;
        dropPlan = null;
        lightZone(null, null);
        return;
    }
    if (Array.isArray(detail.paths) && detail.paths.length) dropPaths = detail.paths.slice();
    if (Number.isFinite(detail.x) && Number.isFinite(detail.y)) {
        dropPoint = {x: detail.x, y: detail.y};
    }
    if (dropPaths.length) planFor(dropPaths);
    relight();
}

function onNativeDropOnField(event) {
    const detail = event.detail || {};
    const paths = Array.isArray(detail.paths) && detail.paths.length
        ? detail.paths.slice() : dropPaths.slice();
    const plan = dropPlan && dropPlan.key === paths.join("\n") ? currentPlan() : null;
    // The shell sends no `leave` after a drop, so whatever was lit goes out
    // here, claimed or not -- and the plan with it: what the drop changes, the
    // next drag has to be asked about again.
    dropPaths = [];
    dropPoint = null;
    dropPlan = null;
    lightZone(null, null);
    if (event.defaultPrevented || !paths.length) return;
    const found = resolveZone(detail.x, detail.y, paths);
    if (!found.zone) return;
    const {zone, spec, verdict} = found;
    event.preventDefault();
    if (verdict.state === "refuse") {
        refuseDrop(zone, verdict.reason);
        return;
    }
    try {
        spec.onDrop(spec.count === "many" ? paths : paths[0],
                    {zone, x: detail.x, y: detail.y, plan});
    } catch (error) {
        console.error("browsePicker: a drop zone's onDrop failed.", error);
    }
}

/** A drop aimed at a box that cannot take it: the box says so where it is,
 *  and a notice says why. Not the navbar's status, which would keep an error
 *  standing for a drop nobody needs to hear about twice. */
function refuseDrop(zone, reason) {
    zone.classList.add("is-drop-refused");
    window.setTimeout(() => {
        if (!(dropLit && dropLit.zone === zone)) zone.classList.remove("is-drop-refused");
    }, 900);
    window.PlexoraToast?.show?.({title: "Not dropped", note: reason, tone: "warning"});
}

/**
 * @function attachBrowseButton - wires a "Browse..." button to fill a text
 * input with the picked path, dispatching an `input` event afterward so any
 * existing onkeyup/oninput live-validation on that field runs unchanged.
 *
 * In the desktop app the field's row also takes a file dropped on it
 * (attachDropTarget): the same box, filled the same way.
 *
 * One button whatever the machine: the explorer takes a file or a folder in
 * mode "any" (see the file header), so there is no File | Folder pair to swap
 * in for it. `accept`, `primary` and `title` go to the explorer; one box
 * takes one path, so it never asks for several.
 */
function attachBrowseButton(buttonEl, inputEl, {mode = "file", filter = "any",
                                                node = null,
                                                examples = null,
                                                accept = null, primary = "",
                                                title = "", drop = true} = {}) {
    if (!buttonEl || !inputEl) return;
    const nodeNow = () => (typeof node === "function" ? node() : node);

    function go(kind) {
        browseForPath({
            mode: kind,
            filter,
            accept,
            primary,
            title,
            // What the chooser hangs under, on a page with no explorer.
            anchorEl: buttonEl,
            examples: examples || filter,
            // Read at click time: the Local/Remote toggle switches which
            // machine this button asks about, on a field already mounted, and
            // the box may have been typed into since this was wired.
            node: nodeNow(),
            start: inputEl.value.trim(),
            // `change` matters most of the three fillPathInput fires: it is
            // the one the Local/Remote switch shares on. Without it, browsing
            // to a file on a cluster filled the box and shared nothing: the
            // form then posted an empty locator and the import answered
            // "provide a valid path to the image file", about a path that was
            // plainly right there.
            onPicked: (path) => fillPathInput(inputEl, path),
            // A dialog that cannot be shown must say so. This used to swallow
            // the error, which turned a rejected filter name into a button
            // that looked ordinary and did nothing when clicked -- with the
            // path input right next to it, there was no way to tell the
            // difference between "no dialog here" and "this button is broken".
            onUnavailable: (error) => {
                console.error("browsePicker: could not open the file browser.", error);
                window.PlexoraStatus?.begin?.("Browse")?.fail?.(
                    error?.message || "Could not open the file browser — type the path instead.");
            },
        });
    }

    buttonEl.addEventListener("click", () => go(mode));
    // `drop: false` for a field whose whole dialog is the target instead
    // (channelNamesUpload.js): a drop there loads the file, not just the box.
    if (!drop) return;

    // The row is the target, not the input alone: the switch and the button
    // beside the box are part of what the pointer is aiming at.
    attachDropTarget(inputEl.closest?.(".import-field-row") || inputEl.parentElement, {
        filter, mode, accept, count: "one",
        enabled: () => !inputEl.disabled && !inputEl.readOnly,
        accepts: () => {
            // A field that draws its own Remote switch (fileSourceRow,
            // channel names) has no handle to move back to This computer, so
            // it says what to do rather than filling a box that means a path
            // on another machine with one from this one.
            const handle = window.PlexoraDataLocation?.handleOf?.(inputEl);
            if (!handle && nodeNow()) {
                return "This box is set to another machine. Switch it to This computer to drop a file from here.";
            }
            return true;
        },
        onDrop: (path) => {
            // A file dragged out of this computer's Explorer or Finder is on
            // this computer, whatever the switch said. Moving it clears the
            // box, which is filled straight after.
            const handle = window.PlexoraDataLocation?.handleOf?.(inputEl);
            if (handle && !handle.isLocal()) handle.setWhere("local", null);
            fillPathInput(inputEl, path);
        },
    });
}
