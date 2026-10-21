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
 * **The machine's own dialog is one step away**, in the explorer's overflow
 * menu ("Use Finder…", "Use File Explorer…", "Use system dialog…"), wherever
 * one exists. POST /browse_path (server/utils/native_dialog.py) opens it on
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
 * so this is reached only from the explorer's "Use system dialog…" on a
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
    if (window.PlexoraPaid?.allows?.("transcriptomics")) return entry;
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
 * The hook the explorer's overflow menu calls: the machine's own file dialog,
 * named for what it is, offered only where `available()` says one exists.
 */
function systemDialogFor({node = null, filter = "any", start = "", anchorEl = null,
                          examples = null} = {}) {
    const desktop = !node && Boolean(window.PlexoraDesktop);
    const platform = desktop ? window.PlexoraDesktop.platform : "";
    let capability = null;
    return {
        get label() {
            if (platform === "windows") return "Use File Explorer…";
            if (platform === "macos" || capability === "hybrid") return "Use Finder…";
            return "Use system dialog…";
        },
        async available() {
            // The desktop app's own dialogs take one kind at a time; the Mac
            // under it has a panel that takes either, through the server.
            capability = desktop
                ? (await serverDialogs() === "hybrid" ? "hybrid" : "kinds")
                : await browseCapability(node);
            return capability;
        },
        async open(kind, {multiple = false, anchor = null} = {}) {
            if (!kind) {
                kind = await chooseKind(anchor || anchorEl, examples || filter);
                if (!kind) return null;
            }
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

/**
 * @function attachBrowseButton - wires a "Browse..." button to fill a text
 * input with the picked path, dispatching an `input` event afterward so any
 * existing onkeyup/oninput live-validation on that field runs unchanged.
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
                                                title = ""} = {}) {
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
            onPicked: (path) => {
                inputEl.value = path;
                inputEl.dispatchEvent(new Event("input"));
                inputEl.dispatchEvent(new Event("keyup"));
                // And `change`, which a programmatic assignment does not fire
                // on its own. It is the one the Local/Remote switch listens
                // for -- sharing a file with another machine is not something
                // to do per keystroke, so it waits for the value to settle.
                // Without this, browsing to a file on a cluster filled the box
                // and shared nothing: the form then posted an empty locator
                // and the import answered "provide a valid path to the image
                // file", about a path that was plainly right there.
                inputEl.dispatchEvent(new Event("change", { bubbles: true }));
            },
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
}
