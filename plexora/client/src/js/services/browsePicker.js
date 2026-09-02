/**
 * browsePicker.js -- shared client for POST /browse_path (see
 * server/utils/native_dialog.py), which pops a native OS file/folder dialog
 * on the machine the server runs on and hands back the chosen path. Used by
 * both the quick-view landing page and the upload page's per-field "Browse..."
 * buttons; only works when there's a real desktop session for the dialog to
 * appear on, so every caller needs an `onUnavailable` fallback (e.g. a manual
 * text input) for the headless/remote-server case.
 *
 * Three answers come back from that route, and the difference between the last
 * two is the whole reason this file is more than a fetch:
 *
 *   a path      the dialog opened and somebody chose something (or cancelled,
 *               which is a null path and not a failure)
 *   "kinds"     there IS a desktop here, but no single dialog takes a file AND
 *               a folder -- every OS but macOS. `chooseKind` asks which, and
 *               the answer opens that machine's own native dialog.
 *   "list"      there is no desktop at all -- a compute node, a container,
 *               notebook mode -- and pathPicker.js's in-app listing stands in.
 *
 * Collapsing "kinds" into "list" is what quietly replaced the Windows and
 * Linux system file browsers with a substitute for one.
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
 * Increasingly the road not taken: `applyCapability` puts these same halves ON
 * the page wherever there is a control to replace, so the question is answered
 * before the click rather than after it. What still arrives here is the case
 * that could not be asked in advance -- the capability probe failed, or the
 * caller passed no control to swap -- and a popup nobody expected is the worst
 * of the three, which is why it is the fallback and not the design.
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
    image: { file: ".ome.tiff · .svs", directory: ".ome.zarr · dicom" },
    mask: { file: ".ome.tiff · .tiff", directory: ".ome.zarr · dicom" },
    data: { file: ".csv · .h5ad", directory: "SpatialData (.zarr)" },
};

/**
 * @function buildSplitControl - the two-half control itself, without deciding
 * where it lives. Used three ways: as the Browse button on a form field, as
 * the landing page's open-an-image panel (`.is-panel`, the same halves at the
 * size of the surface they replace), and as a floating menu for the callers
 * that still have to ask after the fact.
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

    const shown = KIND_EXAMPLES[examples] || {};
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
 * @function browseForPath - opens a native dialog and calls back with the
 * chosen path.
 * @param mode - "any" (a file OR a folder, which is what every path field
 *   wants: an image is an .ome.tif or a .zarr *directory*, and which one it is
 *   is a fact about the format rather than a question to put to the user),
 *   "file", or "directory". Only macOS has an OS dialog that can do "any". On
 *   a Windows or Linux desktop the server answers `fallback: "kinds"` and this
 *   asks which one -- two native dialogs behind one button, rather than the
 *   in-app listing, which is for machines with no desktop to open a dialog on
 *   (`fallback: "list"`) and is a substitute for a system file browser rather
 *   than an improvement on one.
 * @param anchorEl - the button this was launched from, for the "kinds" menu to
 *   hang under. Without one there is nowhere to draw the question, so the
 *   listing picker answers instead -- which still works, and is why a caller
 *   that has no button is not a broken caller.
 * @param filter - narrows the file-type dropdown for mode="file": one of
 *   "image", "csv", "h5ad", or "any" (default -- just "All files"). Ignored
 *   for mode="directory", and by the hybrid panel, which is unfiltered on
 *   purpose (see native_dialog.py). Still read by the listing picker in every
 *   mode, where it greys out files the field cannot take. Must match one of
 *   native_dialog.py's FILTERS keys.
 * @param start - where the listing fallback should open, when it is the one
 *   that runs. Whatever the field already holds, so re-picking a file starts
 *   in the folder it came from rather than back at home. Ignored by the native
 *   dialog, which remembers its own last directory.
 * @param onPicked - called with the chosen path string; not called if the
 *   user cancels the dialog (result.path === null)
 * @param onUnavailable - called (with the error) if the dialog itself
 *   couldn't be shown at all -- no desktop session, tkinter missing, etc.
 */
async function browseForPath({mode = "file", filter = "any", node = null,
                              start = "", anchorEl = null, examples = null,
                              onPicked, onUnavailable} = {}) {
    try {
        const response = await fetch(plexoraUrl("browse_path"), {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            // `node` says WHICH machine's dialog to open. Absent means the one
            // running the server; a name means that node's, which for the node
            // `plexora connect` starts is the user's own computer -- the only
            // machine in the arrangement that reliably has a screen.
            body: JSON.stringify({mode: mode, filter: filter, node: node}),
        });
        const result = await response.json();
        if (!response.ok) {
            // A desktop with single-kind dialogs only -- Windows, Linux. The
            // machine can open a real system file browser, it just cannot ask
            // both questions in one panel, so this asks which and then opens
            // the genuine article. Falls through to the listing when there is
            // no button to hang the menu under.
            if (result.fallback === "kinds" && anchorEl) {
                const kind = await chooseKind(anchorEl, examples || filter);
                // Dismissed. The same answer as cancelling the dialog this
                // would have opened, and it gets the same silence -- an
                // onUnavailable here would report a failure at somebody who
                // simply changed their mind.
                if (!kind) return;
                return browseForPath({mode: kind, filter, node, start,
                                      anchorEl, examples,
                                      onPicked, onUnavailable});
            }
            // "There is no desktop here" is not a failure to report at
            // somebody: it is the ordinary state of a compute node, and the
            // listing picker is the answer. The server says so in a field
            // rather than in prose so this can act on it.
            if ((result.fallback === "list" || result.fallback === "kinds")
                    && window.PlexoraPathPicker) {
                // Listed on the same machine the dialog would have opened on.
                // Passing `node` through is what makes Browse work at all for
                // a field pointed at a cluster: that host has no desktop, so
                // this branch is the ONLY one it ever takes, and a listing of
                // the wrong machine's filesystem would be worse than none.
                const picked = await window.PlexoraPathPicker.pick({
                    mode,
                    filter,
                    node,
                    // Opens where the field is already pointing. The server
                    // turns a file's path into the folder that holds it, so
                    // correcting a mistyped filename does not start over at
                    // home -- which on a cluster is nowhere near the data.
                    start,
                    title: {
                        directory: `Choose a folder on ${node || "the server"}`,
                        any: `Choose a file or folder on ${node || "the server"}`,
                    }[mode] || `Choose a file on ${node || "the server"}`,
                });
                if (picked && onPicked) onPicked(picked);
                return;
            }
            throw new Error(result.error);
        }
        if (result.path && onPicked) {
            onPicked(result.path);
        }
        // result.path === null just means the user cancelled the dialog.
    } catch (error) {
        if (onUnavailable) {
            onUnavailable(error);
        }
    }
}

/**
 * @function attachBrowseButton - wires a "Browse..." button to fill a text
 * input with the picked path, dispatching an `input` event afterward so any
 * existing onkeyup/oninput live-validation on that field runs unchanged.
 *
 * On a machine whose native dialogs are single-kind, the button is REPLACED by
 * the File/Folder control rather than opening it -- see `applyCapability`.
 */
function attachBrowseButton(buttonEl, inputEl, {mode = "file", filter = "any",
                                                node = null,
                                                examples = null} = {}) {
    if (!buttonEl || !inputEl) return;
    const nodeNow = () => (typeof node === "function" ? node() : node);

    function go(kind) {
        browseForPath({
            mode: kind,
            filter,
            // What the chooser hangs under, for the callers that still open it
            // as a popup rather than having been replaced by it.
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
                console.error("browsePicker: could not open the file dialog.", error);
                window.PlexoraStatus?.begin?.("Browse")?.fail?.(
                    error?.message || "Could not open the file browser — type the path instead.");
            },
        });
    }

    buttonEl.addEventListener("click", () => go(mode));

    // Only mode "any" has two answers to offer. A field that wants a file, or
    // a folder, has nothing to split -- one button already says which.
    if (mode === "any") {
        applyCapability(buttonEl, inputEl, examples || filter, nodeNow, go);
    }
}

/**
 * @function applyCapability - decides which control this field gets, and swaps
 * it in place when the answer changes.
 *
 * A machine with one dialog that takes either kind (macOS) keeps the single
 * Browse button: two halves there would invent a decision its OS does not
 * need. A machine with two single-kind dialogs -- Windows, Linux -- gets the
 * File/Folder control INSTEAD of the button, because on those the question is
 * unavoidable and asking it up front is one click rather than two.
 *
 * A machine with no desktop keeps the single button too: both halves would
 * open the same in-app listing, which already takes either kind.
 *
 * Re-run on `change`, which is what the Local/Remote switch fires. That switch
 * can point a mounted field at a different computer, and the control has to
 * follow it -- a laptop's File/Folder pair left over on a field now aimed at a
 * cluster would offer two routes to the same listing.
 *
 * `buttonEl` need not be a button. The landing page hands this its open-an-image
 * panel, which is a surface rather than a control but is replaced by exactly the
 * same halves at exactly the same moment, and for the same reason. `variant` is
 * the class that sizes them for it.
 */
function applyCapability(buttonEl, inputEl, examples, nodeNow, go,
                         {variant = null} = {}) {
    let split = null;
    let showing = null;

    function render(kind) {
        if (kind === showing) return;
        showing = kind;
        if (kind === "kinds") {
            if (!split) {
                split = buildSplitControl(examples, go);
                if (variant) split.classList.add(variant);
                split.addEventListener("keydown", (event) => {
                    stepBetweenHalves(split, event);
                });
            }
            if (!split.isConnected) buttonEl.after(split);
            buttonEl.hidden = true;
        } else {
            if (split && split.isConnected) split.remove();
            buttonEl.hidden = false;
        }
    }

    function ask() {
        const at = nodeNow();
        // The button stands until an answer arrives. It is the control that
        // works everywhere -- a field that swapped to halves and back on a slow
        // answer would move under the pointer.
        browseCapability(at).then((kind) => {
            if (nodeNow() === at) render(kind);
        }).catch(() => render(null));
    }

    ask();
    inputEl.addEventListener("change", ask);
}
