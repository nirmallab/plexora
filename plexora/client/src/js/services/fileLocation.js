/**
 * PlexoraFileLocation -- "which machine?", asked of every file button at once.
 *
 * dataLocation.js asks this question per FIELD, by building a switch into the
 * field. That works for the handful of core import forms it was written for and
 * for nothing else: every plugin has its own Upload arrow, its own Download
 * button, its own hidden `<input type="file">`, and none of them knows the
 * switch exists. So on a session whose data lives on a cluster, every one of
 * those buttons quietly meant "the laptop" -- the one machine the data is not
 * on.
 *
 * Rather than teach each plugin the question, this asks it at the only place
 * they all pass through: the click. A file input about to open an OS dialog and
 * an `<a download>` about to save into Downloads are both intercepted here, the
 * user picks a machine, and the plugin's own workflow then continues with the
 * bytes coming from -- or going to -- wherever they said. No plugin form
 * changes, and a plugin written next year is covered without knowing this file
 * exists.
 *
 * **Nothing happens when there is nowhere else to go.** With no reachable
 * remote machine there is no question worth asking, so the click is not touched
 * at all and every button behaves exactly as it did. That is also the fail-safe
 * for a snapshot that has not loaded yet: unknown means local.
 *
 * **The check has to be synchronous.** It happens inside a click handler, and
 * `preventDefault()` cannot wait for a fetch -- by the time an answer came back
 * the native picker would already be open. So the reachable list is kept
 * current by a passive subscription to PlexoraRemotes (which is polling anyway
 * for the navbar globe) and read out of a variable here.
 *
 * **Two things cannot be intercepted, and they call in instead.** A form
 * submitted with `form.submit()` fires no event, and a Blob built in the tab
 * and saved through a detached anchor never reaches this document. Both are
 * real -- they are how the two largest exports in the app work -- so the layer
 * offers `deliver(blob, filename)` as the way in, and that is the documented
 * contract for anything a plugin builds client-side:
 *
 *     await PlexoraFileLocation.deliver(blob, "gated_cells.csv");
 *
 * **Opting out.** `data-file-location="local"` on an input, an anchor, or any
 * ancestor of one means "this one is always this computer" -- used by the core
 * fields that already have their own Local/Remote switch, so they do not ask
 * the same question twice in two different shapes.
 */
window.PlexoraFileLocation = (function () {
    "use strict";

    const Remotes = () => window.PlexoraRemotes;
    const Picker = () => window.PlexoraPathPicker;

    //: What `askLocation` resolves with for the browser's own machine. A
    //: sentinel rather than a place object because it is not one: "local" is
    //: the machine the DOM is on, which no `/data_places` entry describes.
    const LOCAL = "local";

    //: The filters pathPicker knows, tried in order of how specific they are.
    //: Only used to translate an `accept` attribute into one of them -- see
    //: `filterFor`, which falls back to "any" the moment it is unsure.
    const FILTERS = ["csv", "h5ad", "channels", "image", "data"];

    //: Every machine a file could be on right now, refreshed by the
    //: subscription below. Read synchronously by `remoteAvailable`.
    let places = [];
    //: Whether a snapshot has ever arrived. Before one has, there is no honest
    //: answer to "is anything reachable", and the honest fallback is today's
    //: behaviour rather than a dialog listing nothing.
    let loaded = false;

    //: What was chosen last time, offered as the starting point for the next
    //: button. Same reasoning as dataLocation.js's `lastPlace`: somebody
    //: exporting three figures in a row is answering one question three times.
    let lastPlaceId = null;
    let lastWasLocal = false;

    //: Elements this layer is deliberately re-clicking, so its own synthetic
    //: click is not intercepted a second time. A WeakSet rather than an
    //: attribute: an attribute would be visible to the plugin's own code, and
    //: a flag left behind by an interrupted flow would silently disable the
    //: layer for that button forever.
    const bypass = new WeakSet();

    //: One dialog at a time. Two file buttons cannot be pressed at once, but a
    //: `deliver()` racing an intercepted click can, and two stacked modals
    //: over a viewer make both unreadable.
    let openDialog = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(className, text, onClick) {
        const node = el("button", className, text);
        node.type = "button";
        if (onClick) node.addEventListener("click", onClick);
        return node;
    }

    function url(path) {
        return (typeof plexoraUrl === "function") ? plexoraUrl(path) : "/" + path;
    }

    // -- what counts as somewhere else -------------------------------------

    /**
     * The places a file could be read from or written to right now.
     *
     * `registered_node` as well as `node`, and it matters: a data node outlives
     * the Plexora that started it, so after a server restart `node` is empty
     * for a machine that is up and answering. Filtering on `node` alone -- as
     * dataLocation.onlyPlace still does for its own narrower purpose -- makes
     * a perfectly reachable cluster invisible the morning after.
     */
    function reachable(snapshot) {
        return ((snapshot && snapshot.places) || []).filter(
            (place) => place.kind === "server"
                       || Boolean(place.node) || Boolean(place.registered_node));
    }

    /** The node name to address a place by, "" for the server's own disk. */
    function nodeOf(place) {
        if (!place || place === LOCAL) return "";
        return place.node || place.registered_node || "";
    }

    /**
     * @function remoteAvailable - is there anywhere else to put this?
     *
     * Synchronous by construction: every caller is inside a click handler, and
     * an answer that arrives a tick later arrives after the browser has already
     * opened the file dialog.
     */
    function remoteAvailable() {
        return loaded && places.length > 0;
    }

    // -- the dialog ---------------------------------------------------------

    /**
     * Ask which machine, and resolve with LOCAL, a place, or null.
     *
     * `onLocal` runs SYNCHRONOUSLY inside the row's own click handler, before
     * this promise resolves, and that is not a style choice. Opening a file
     * dialog needs transient user activation, which a promise callback two
     * tasks later may no longer have -- so the one thing that must happen in
     * the gesture happens in the gesture, and the promise is only how the
     * caller learns what was decided.
     */
    function askLocation({ title, intent, localName, localDetail,
                           onLocal = null } = {}) {
        if (openDialog) return Promise.resolve(null);

        const dialog = el("dialog", "connect-modal file-location-modal");
        const head = el("div", "connect-modal-head");
        const heading = el("div", "connect-modal-heading");
        heading.append(el("h2", "connect-modal-title", title));
        heading.append(el("p", "connect-modal-subtitle", intent || ""));
        head.append(heading);

        const body = el("div", "connect-modal-body");
        const list = el("div", "file-location-list");
        let outcome = null;

        function row(name, detail, chip, onPick) {
            const item = button("connect-modal-row file-location-row", null, onPick);
            const main = el("div", "connect-modal-row-main");
            main.append(el("span", "connect-modal-row-name", name));
            main.append(el("span", "connect-modal-row-detail", detail || ""));
            item.append(main);
            if (chip) item.append(el("span", "connect-modal-chip is-ready", chip));
            list.append(item);
            return item;
        }

        const here = row(localName || "This computer",
                         localDetail || "The machine you are sitting at.",
                         null, () => {
            outcome = LOCAL;
            lastWasLocal = true;
            // Closed first, so the picker the callback opens is not layered
            // over a modal that is about to vanish underneath it. `close()`
            // takes the dialog out of the top layer synchronously; only its
            // `close` EVENT is queued, which is what resolves the promise.
            if (dialog.open) dialog.close();
            if (onLocal) onLocal();
        });

        places.forEach((place) => {
            const item = row(place.label, place.detail || "",
                             place.kind === "server" ? "server" : "connected",
                             () => {
                outcome = place;
                lastWasLocal = false;
                lastPlaceId = place.id;
                if (dialog.open) dialog.close();
            });
            if (!lastWasLocal && place.id === lastPlaceId) {
                item.classList.add("is-last-used");
            }
        });

        body.append(list);

        const actions = el("div", "connect-modal-actions");
        actions.append(button("btn btn-outline-light", "Cancel",
                              () => { if (dialog.open) dialog.close(); }));
        actions.append(el("span", "connect-modal-spacer"));
        // The escape hatch the list cannot offer: a machine that is saved but
        // not open, or one that is not saved at all. Opening it from here is
        // the same flow the data fields use, and it comes back to this dialog
        // with the new machine already in the list.
        if (window.PlexoraConnectionModal) {
            actions.append(button("btn btn-secondary", "Connect another machine…",
                                  () => {
                outcome = "connect";
                if (dialog.open) dialog.close();
            }));
        }

        dialog.append(head, body, actions);
        document.body.appendChild(dialog);
        openDialog = dialog;

        return new Promise((resolve) => {
            // Resolved solely here, so Escape, the Cancel button and a row all
            // arrive at one place -- and the promise cannot be left pending by
            // a path that closed the dialog some other way.
            dialog.addEventListener("close", () => {
                dialog.remove();
                if (openDialog === dialog) openDialog = null;
                if (outcome === "connect") {
                    resolve(connectThenAsk({ title, intent, localName,
                                             localDetail, onLocal }));
                    return;
                }
                resolve(outcome);
            });
            dialog.showModal();
            const preferred = list.querySelector(".is-last-used")
                || (lastWasLocal ? here : list.firstChild);
            if (preferred && preferred.focus) preferred.focus();
        });
    }

    /** Open a machine, then ask again with it in the list. */
    async function connectThenAsk(options) {
        const opened = await window.PlexoraConnectionModal.open({
            kind: "node",
            intent: "Opening a data node here makes this machine's files "
                    + "available to every upload and download in Plexora.",
        });
        if (opened && opened.connected) {
            // The snapshot the modal's own work produced, rather than the one
            // this layer was holding when the dialog opened.
            await refresh();
        }
        if (!places.length) return null;
        return askLocation(options);
    }

    /**
     * The name to save under, asked after the folder.
     *
     * Two screens rather than one because they are two different widgets: the
     * folder is a filesystem to walk (pathPicker, which already knows how to do
     * that on a machine with no desktop) and the name is a text box. `error`
     * with `offerReplace` is how a refused overwrite comes back -- the same
     * dialog, redrawn, with the button that says what would happen.
     */
    function askName({ place, directory, name, error = "",
                       offerReplace = false }) {
        if (openDialog) return Promise.resolve(null);

        const dialog = el("dialog", "connect-modal file-location-modal");
        const head = el("div", "connect-modal-head");
        const heading = el("div", "connect-modal-heading");
        heading.append(el("h2", "connect-modal-title", "Save as"));
        heading.append(el("p", "connect-modal-subtitle",
                          directory + "  on " + place.label));
        head.append(heading);

        const body = el("div", "connect-modal-body");
        const field = el("input", "form-control file-location-name");
        field.type = "text";
        field.value = name || "";
        field.setAttribute("aria-label", "File name");
        body.append(field);
        if (error) {
            const warned = el("p", "connect-modal-error", error);
            warned.removeAttribute("hidden");
            body.append(warned);
        }

        let answer = null;
        function accept(overwrite) {
            const chosen = field.value.trim();
            if (!chosen) return;
            answer = { name: chosen, overwrite: Boolean(overwrite) };
            if (dialog.open) dialog.close();
        }

        const actions = el("div", "connect-modal-actions");
        actions.append(button("btn btn-outline-light", "Cancel",
                              () => { if (dialog.open) dialog.close(); }));
        actions.append(el("span", "connect-modal-spacer"));
        actions.append(button("btn btn-primary",
                              offerReplace ? "Replace" : "Save",
                              () => accept(offerReplace)));

        field.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                accept(offerReplace);
            }
        });

        dialog.append(head, body, actions);
        document.body.appendChild(dialog);
        openDialog = dialog;

        return new Promise((resolve) => {
            dialog.addEventListener("close", () => {
                dialog.remove();
                if (openDialog === dialog) openDialog = null;
                resolve(answer);
            });
            dialog.showModal();
            field.focus();
            // The stem, not the suffix: somebody renaming `export.csv` means
            // to change `export`, and selecting the whole string makes the
            // `.csv` something they have to type again.
            const dot = field.value.lastIndexOf(".");
            if (field.setSelectionRange) {
                field.setSelectionRange(0, dot > 0 ? dot : field.value.length);
            }
        });
    }

    // -- moving the bytes ---------------------------------------------------

    function tail(path) {
        // Both separators, because the far side may be a Windows box and this
        // one may not -- the same reason dir_listing.py builds paths server
        // side rather than letting the browser guess.
        const text = String(path || "").replace(/\\/g, "/").replace(/\/+$/, "");
        return text.split("/").pop() || "download";
    }

    /** A route's JSON, or `{}` for one that answered with something else. */
    async function said(response) {
        try {
            return (await response.json()) || {};
        } catch (e) {
            return {};
        }
    }

    /**
     * @function read - one file from a machine, as a File.
     *
     * Public, because the interception is not the only way this question gets
     * asked. A surface that already carries its own Local/Remote switch --
     * views/channelNamesUpload.js -- has answered it before any click happens
     * and needs the bytes, not a dialog. Better one caller than a second copy
     * of the route name and the header it answers with.
     */
    async function fetchFile(node, path) {
        const response = await fetch(url("fetch_file"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ node: node, path: path }),
        });
        if (!response.ok) {
            throw new Error((await said(response)).error
                            || "That file could not be read.");
        }
        const name = response.headers.get("X-Plexora-File-Name") || tail(path);
        const blob = await response.blob();
        return new File([blob], name,
                        { type: blob.type || "application/octet-stream" });
    }

    /** One file onto a machine. `{ok}`, or `{exists}` for a refused overwrite. */
    async function putFile(node, directory, name, blob, overwrite) {
        const form = new FormData();
        form.append("file", blob, name);
        form.append("node", node);
        form.append("dir", directory);
        form.append("name", name);
        if (overwrite) form.append("overwrite", "1");
        const response = await fetch(url("put_file"),
                                     { method: "POST", body: form });
        const answer = await said(response);
        if (response.ok) return { ok: true, path: answer.path || "" };
        return { ok: false, exists: Boolean(answer.exists),
                 error: answer.error || "That file could not be saved." };
    }

    /**
     * Folder, then name, then the write -- looping while the name is taken.
     *
     * The loop is the point: a refused overwrite is a question, and the answer
     * is either a different name or "yes, replace it". Both come back through
     * the same screen, so neither is a dead end somebody has to start over from.
     */
    async function saveOnPlace(place, blob, filename) {
        if (!Picker()) return false;
        const node = nodeOf(place);
        const directory = await Picker().pick({
            mode: "directory", node: node,
            title: "Where on " + place.label + "?",
        });
        if (!directory) return false;

        let name = filename;
        let error = "";
        let offerReplace = false;
        for (;;) {
            const chosen = await askName({ place, directory, name, error,
                                           offerReplace });
            if (!chosen) return false;
            name = chosen.name;
            const written = await track(
                "Saving", putFile(node, directory, name, blob, chosen.overwrite));
            if (written.ok) {
                note(name + " saved to " + place.label + ".");
                return true;
            }
            error = written.error;
            offerReplace = written.exists;
        }
    }

    function track(label, promise) {
        if (window.PlexoraStatus && window.PlexoraStatus.track) {
            return window.PlexoraStatus.track(label, promise);
        }
        return promise;
    }

    function note(message) {
        if (window.PlexoraStatus && window.PlexoraStatus.begin) {
            const task = window.PlexoraStatus.begin(message);
            window.setTimeout(() => task.done(), 1500);
        }
    }

    function complain(message) {
        if (window.PlexoraStatus && window.PlexoraStatus.setError) {
            window.PlexoraStatus.setError(String(message).slice(0, 80));
        } else if (window.console) {
            window.console.error(message);
        }
    }

    // -- the two things a click can be --------------------------------------

    /**
     * Which of pathPicker's filters matches this input's `accept`, if any.
     *
     * Conservative on purpose. Greying out a file the form would have taken is
     * a dead end with no way past it inside the picker, while offering one it
     * refuses costs a sentence from the form's own validation -- so anything
     * this is not certain about is "any". The test is run through the picker's
     * own `accepts`, rather than against a fourth copy of the suffix table.
     */
    function filterFor(input) {
        const raw = String((input && input.accept) || "").toLowerCase();
        const parts = raw.split(",").map((each) => each.trim()).filter(Boolean);
        if (!parts.length) return "any";
        // The one MIME shorthand worth translating: a picker filter exists for
        // exactly this, and it is what every "add an image" field asks for.
        if (parts.length === 1 && parts[0] === "image/*") return "image";
        // Anything else with a MIME type in it means suffixes are not the
        // whole story, and guessing past that greys out files the form takes.
        if (parts.some((each) => each.charAt(0) !== ".")) return "any";
        const picker = Picker();
        if (!picker || typeof picker.accepts !== "function") return "any";
        for (const name of FILTERS) {
            if (parts.every((suffix) => picker.accepts("file" + suffix, name))) {
                return name;
            }
        }
        return "any";
    }

    /** An upload button, asked where the file is. */
    async function askUpload(input) {
        const choice = await askLocation({
            title: "Where is the file?",
            intent: "Plexora can open it from this computer or from a machine "
                    + "you are connected to.",
            localDetail: "Opens the usual file dialog.",
            onLocal: () => {
                bypass.add(input);
                input.click();
            },
        });
        if (!choice || choice === LOCAL || !Picker()) return;

        const picked = await Picker().pick({
            mode: "file", filter: filterFor(input), node: nodeOf(choice),
            multiple: Boolean(input.multiple),
            title: "Choose a file on " + choice.label,
        });
        if (!picked) return;
        const paths = Array.isArray(picked) ? picked : [picked];
        if (!paths.length) return;

        let files;
        try {
            files = await track("Fetching", Promise.all(
                paths.map((path) => fetchFile(nodeOf(choice), path))));
        } catch (e) {
            complain(e.message || e);
            return;
        }

        // The plugin's own handler, reached exactly as a real file dialog
        // would reach it: the files are on the input, and `change` fires. What
        // happens next is the plugin's code, unchanged and unaware.
        const transfer = new DataTransfer();
        files.forEach((file) => transfer.items.add(file));
        input.files = transfer.files;
        input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    /** A download link, asked where the file should go. */
    async function askDownload(anchor) {
        const href = anchor.getAttribute("href");
        const suggested = anchor.getAttribute("download") || tail(href);
        const choice = await askLocation({
            title: "Where should it be saved?",
            intent: "Downloads go to this computer unless you send them to a "
                    + "machine you are connected to.",
            localDetail: "Saves to your browser's downloads folder.",
            onLocal: () => {
                bypass.add(anchor);
                anchor.click();
            },
        });
        if (!choice || choice === LOCAL) return;

        let blob;
        try {
            const response = await track("Downloading", fetch(anchor.href));
            if (!response.ok) throw new Error("The download failed.");
            blob = await response.blob();
        } catch (e) {
            complain(e.message || e);
            return;
        }
        await saveOnPlace(choice, blob, suggested);
    }

    /**
     * @function deliver - hand a file to the user, wherever they want it.
     *
     * The way in for anything built in the tab: a CSV assembled from a
     * selection, a figure rendered to a canvas, an export a hidden form used to
     * stream. Those never reach the click interception -- a `form.submit()`
     * fires no event and a detached anchor never bubbles to this document -- so
     * they call this instead, and get the same question everything else gets.
     *
     * With nowhere else to send it this saves locally WITHOUT touching the
     * network. That matters more than it looks: one caller is an emergency
     * export offered precisely when the server has stopped answering, and a
     * "where would you like this?" that needs a live server to answer would
     * fail exactly when it was needed.
     */
    async function deliver(blob, filename) {
        const name = String(filename || "download");
        if (!remoteAvailable()) {
            saveLocally(blob, name);
            return true;
        }
        let done = false;
        const choice = await askLocation({
            title: "Where should “" + name + "” be saved?",
            intent: "This computer, or a machine you are connected to.",
            localDetail: "Saves to your browser's downloads folder.",
            onLocal: () => { saveLocally(blob, name); done = true; },
        });
        if (choice === LOCAL) return done;
        if (!choice) return false;
        return saveOnPlace(choice, blob, name);
    }

    /**
     * The browser's own save. An anchor, a click, and an object URL.
     *
     * Added to the bypass BEFORE the click, because this anchor is attached to
     * the document and would otherwise be caught by the interception below --
     * which would ask the question that has just been answered, forever.
     */
    function saveLocally(blob, filename) {
        const href = URL.createObjectURL(blob);
        const anchor = el("a");
        anchor.href = href;
        anchor.download = filename;
        anchor.style.display = "none";
        bypass.add(anchor);
        document.body.appendChild(anchor);
        anchor.click();
        // Revoked late rather than immediately: Safari has historically read
        // the URL after the click returns, and a URL revoked too early is a
        // download that silently produces nothing.
        window.setTimeout(() => {
            URL.revokeObjectURL(href);
            anchor.remove();
        }, 30000);
    }

    // -- the interception ---------------------------------------------------

    /** Whether this element has been told to stay on this computer. */
    function optedOut(element) {
        return Boolean(element.closest
                       && element.closest('[data-file-location="local"]'));
    }

    /**
     * One listener for every file button on the page, in the bubble phase.
     *
     * Bubble rather than capture, for the same reason appRouter's link
     * interception is: a handler that has already called `preventDefault()` has
     * decided what this click means, and a layer that ran first would take that
     * decision away from it. Bubble is still in time -- a click's default
     * behaviour runs after the event has finished propagating.
     */
    function onClick(event) {
        if (event.defaultPrevented) return;
        if (event.button) return;
        // A modified click is the user asking their BROWSER for something --
        // open in a new tab, save as -- and intercepting it would be answering
        // a question they did not ask this application.
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        const target = event.target;
        if (!target || !target.closest) return;

        const input = target.closest('input[type="file"]');
        if (input) return maybe(event, input, askUpload);

        const anchor = target.closest("a[download]");
        if (anchor) {
            const href = anchor.getAttribute("href");
            if (!href || href.charAt(0) === "#") return;
            maybe(event, anchor, askDownload);
        }
    }

    function maybe(event, element, handler) {
        if (bypass.has(element)) {
            // One re-click each: this is the layer's own synthetic click going
            // past, and leaving the mark would disable the button next time.
            bypass.delete(element);
            return;
        }
        if (optedOut(element) || !remoteAvailable()) return;
        event.preventDefault();
        handler(element);
    }

    // -- staying current ----------------------------------------------------

    function onSnapshot(snapshot) {
        loaded = Boolean(snapshot && snapshot.loaded);
        places = reachable(snapshot);
    }

    async function refresh() {
        if (!Remotes()) return;
        try {
            onSnapshot(await Remotes().refresh());
        } catch (e) {
            /* Leave the last good list; a failed poll is not an answer. */
        }
    }

    /**
     * Start watching, once per process.
     *
     * Passively, like the navbar globe and sessionExpiry: this must not turn a
     * settled connection into a request per second for the privilege of knowing
     * whether a dialog is worth showing.
     */
    let started = false;
    function start() {
        if (started) return;
        started = true;
        document.addEventListener("click", onClick);
        if (Remotes()) Remotes().subscribe(onSnapshot);
    }

    return { start, remoteAvailable, deliver, read: fetchFile, LOCAL };
})();

// At parse time rather than through PlexoraPage: the listener is on `document`
// and survives every routed page swap, so registering per page would add a
// second one on the first navigation. The guard inside start() makes that safe
// either way.
window.PlexoraFileLocation.start();
