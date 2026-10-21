/**
 * explorerCrumbs.js -- the explorer's navigation row: Back, Forward, Up,
 * Reload, Home, the path as a single line of clickable folders, and the star
 * that pins the folder that is open.
 *
 * The path is the server's (`payload.crumbs`, one `{label, path}` per step),
 * so a Windows node's `C:\` and a cluster's `/n/scratch` draw the same way and
 * a click goes to exactly the path that crumb carries. Nothing here joins or
 * splits a path.
 *
 * One line, always. When the trail is wider than the bar, the folders nearest
 * the root (after the root itself) fold into a "…" button whose menu lists
 * them, and if a single name is still too long the strip scrolls to its end.
 *
 * Clicking the bar anywhere that is not a crumb -- or Cmd/Ctrl+L -- turns it
 * into a text box holding the path, selected, because pasting a path and
 * pressing Enter is how somebody who knows where their data is gets there.
 */
window.PlexoraExplorerCrumbs = (function () {
    "use strict";

    const T = () => window.PlexoraExplorerTypes;

    function create({ onGo = () => {}, onBack = () => {}, onForward = () => {},
                      onUp = () => {}, onRefresh = () => {}, onHome = () => {},
                      onPin = () => {}, onContextMenu = () => {} } = {}) {
        const { el, button, glyph, setIcon } = T();

        const state = {
            here: "",
            crumbs: null,
            editing: false,
            folded: [],
        };

        const element = el("div", "plx-explorer-navrow");

        function tool(action, icon, title) {
            const node = button("plx-explorer-tool", "", title);
            node.setAttribute("data-action", action);
            node.append(glyph(icon));
            return node;
        }

        const back = tool("back", "fas fa-arrow-left", "Back (Alt+←)");
        const forward = tool("forward", "fas fa-arrow-right", "Forward (Alt+→)");
        const up = tool("up", "fas fa-arrow-up", "Enclosing folder (Backspace)");
        const refresh = tool("refresh", "fas fa-arrows-rotate", "Reload this folder");
        const home = tool("home", "fas fa-house", "Home folder");
        const tools = el("div", "plx-explorer-tools");
        tools.append(back, forward, up, refresh, home);

        const address = el("div", "plx-explorer-address");
        const trail = el("nav", "plx-explorer-crumbs");
        trail.setAttribute("aria-label", "Path");
        const edit = el("input", "plx-explorer-crumb-edit");
        edit.type = "text";
        edit.setAttribute("spellcheck", "false");
        edit.setAttribute("autocomplete", "off");
        edit.setAttribute("aria-label", "Folder path");
        edit.setAttribute("placeholder", "Type or paste a path, then press Enter");
        edit.hidden = true;
        const stretch = button("plx-explorer-crumb-open", "", "Type or paste a path (Ctrl+L)");
        stretch.setAttribute("aria-label", "Type or paste a path");
        stretch.append(glyph("fas fa-pencil"));
        address.append(trail, stretch, edit);

        const pin = button("plx-explorer-pin", "", "Pin this folder to Favorites");
        pin.setAttribute("data-action", "pin");
        pin.setAttribute("aria-pressed", "false");
        const pinGlyph = glyph("far fa-star");
        pin.append(pinGlyph);

        element.append(tools, address, pin);

        //: Under the row, not inside it: a sentence does not fit in a bar
        //: that is one folder name high.
        const messages = el("div", "plx-explorer-messages");
        const error = el("div", "plx-explorer-address-error");
        error.setAttribute("role", "alert");
        const note = el("div", "plx-explorer-note");
        messages.append(error, note);

        // -- drawing ---------------------------------------------------------

        let more = null;
        let moreSep = null;
        //: Each crumb with the separator after it, so folding hides both.
        let steps = [];

        function separator() {
            const sep = glyph("fas fa-chevron-right", "plx-explorer-crumb-sep");
            return sep;
        }

        function renderTrail() {
            trail.replaceChildren();
            more = null;
            moreSep = null;
            steps = [];
            if (!state.crumbs) return;
            const last = state.crumbs.length - 1;
            state.crumbs.forEach((crumb, index) => {
                const step = button("plx-explorer-crumb", crumb.label, crumb.path);
                step.setAttribute("data-index", String(index));
                if (index === last) step.setAttribute("aria-current", "location");
                step.addEventListener("click", (event) => {
                    // A crumb is somewhere to go. Without this the click also
                    // reaches the bar behind it, which would open the text box
                    // over the folder that was just asked for.
                    if (event && event.stopPropagation) event.stopPropagation();
                    onGo(crumb.path);
                });
                step.addEventListener("contextmenu", (event) => {
                    if (event.preventDefault) event.preventDefault();
                    if (event.stopPropagation) event.stopPropagation();
                    onContextMenu(crumb, { x: event.clientX || 0, y: event.clientY || 0 });
                });
                trail.append(step);
                const record = { node: step, sep: null };
                steps.push(record);
                if (index === 0 && last > 0) {
                    record.sep = separator();
                    trail.append(record.sep);
                    more = button("plx-explorer-crumb-more", "", "Earlier folders");
                    more.append(glyph("fas fa-ellipsis"));
                    more.hidden = true;
                    more.addEventListener("click", (event) => {
                        if (event && event.stopPropagation) event.stopPropagation();
                        openFolded();
                    });
                    moreSep = separator();
                    moreSep.hidden = true;
                    trail.append(more, moreSep);
                } else if (index < last) {
                    record.sep = separator();
                    trail.append(record.sep);
                }
            });
            fit();
        }

        /**
         * Fold leading folders into "…" until the trail fits on its line. The
         * root stays (it says which machine-shaped tree this is) and so does
         * the folder that is open.
         */
        function fit() {
            steps.forEach(({ node, sep }) => {
                node.hidden = false;
                if (sep) sep.hidden = false;
            });
            state.folded = [];
            if (more) { more.hidden = true; moreSep.hidden = true; }
            if (!trail.clientWidth || !more) return;
            let index = 1;
            while (trail.scrollWidth > trail.clientWidth && index < steps.length - 1) {
                steps[index].node.hidden = true;
                if (steps[index].sep) steps[index].sep.hidden = true;
                state.folded.push(state.crumbs[index]);
                more.hidden = false;
                moreSep.hidden = false;
                index += 1;
            }
            if (trail.scrollWidth > trail.clientWidth) trail.scrollLeft = trail.scrollWidth;
        }

        function openFolded() {
            if (!state.folded.length || !window.PlexoraMenu) return;
            window.PlexoraMenu.open(more, state.folded.map((crumb) => ({
                label: crumb.label,
                title: crumb.path,
                onSelect: () => onGo(crumb.path),
            })), { align: "left" });
        }

        // -- the text box ------------------------------------------------------

        function beginEdit({ focus = true } = {}) {
            state.editing = true;
            edit.value = state.here;
            edit.hidden = false;
            trail.hidden = true;
            stretch.hidden = true;
            address.classList.add("is-editing");
            if (focus) {
                if (edit.focus) edit.focus();
                // Selected, so the next thing typed or pasted replaces the
                // path rather than landing in the middle of it.
                if (edit.select) edit.select();
            }
        }

        function endEdit() {
            // A node too old to send crumbs keeps the box: it is all there is.
            if (!state.crumbs) return;
            state.editing = false;
            edit.hidden = true;
            trail.hidden = false;
            stretch.hidden = false;
            address.classList.remove("is-editing");
            fit();
        }

        // -- what the controller tells it ---------------------------------------

        function setLocation({ here = "", crumbs = null } = {}) {
            state.here = here || "";
            state.crumbs = Array.isArray(crumbs) && crumbs.length ? crumbs : null;
            edit.value = state.here;
            renderTrail();
            if (state.crumbs) {
                if (state.editing) endEdit();
            } else {
                beginEdit({ focus: false });
            }
        }

        function setHistory({ back: canBack = false, forward: canForward = false,
                              up: canUp = false } = {}) {
            back.disabled = !canBack;
            forward.disabled = !canForward;
            up.disabled = !canUp;
        }

        function setPinned(on, enabled = true) {
            pin.disabled = !enabled;
            pin.setAttribute("aria-pressed", on ? "true" : "false");
            const title = on ? "Unpin this folder from Favorites" : "Pin this folder to Favorites";
            pin.setAttribute("title", title);
            pin.setAttribute("aria-label", title);
            pin.classList.toggle("is-on", Boolean(on));
            setIcon(pinGlyph, on ? "fas fa-star" : "far fa-star");
        }

        function setError(message) {
            error.textContent = message || "";
            error.hidden = !message;
            address.classList.toggle("is-invalid", Boolean(message));
            if (/permission/i.test(message || "")) error.classList.add("is-permission");
            else error.classList.remove("is-permission");
        }

        function setNote(message) {
            note.textContent = message || "";
            note.hidden = !message;
        }

        // -- wiring ----------------------------------------------------------

        back.addEventListener("click", () => onBack());
        forward.addEventListener("click", () => onForward());
        up.addEventListener("click", () => onUp());
        refresh.addEventListener("click", () => onRefresh());
        home.addEventListener("click", () => onHome());
        pin.addEventListener("click", () => onPin());

        stretch.addEventListener("click", (event) => {
            if (event && event.stopPropagation) event.stopPropagation();
            beginEdit();
        });
        // Anywhere in the strip that is not a crumb. The guard is what lets
        // somebody click into the middle of the path they are already editing
        // without the whole thing being re-selected under them.
        address.addEventListener("click", () => {
            if (!state.editing) beginEdit();
        });
        address.addEventListener("contextmenu", (event) => {
            if (state.editing) return;
            if (event.preventDefault) event.preventDefault();
            onContextMenu(null, { x: event.clientX || 0, y: event.clientY || 0 });
        });

        edit.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                if (event.stopPropagation) event.stopPropagation();
                const typed = (edit.value || "").trim();
                if (typed) onGo(typed);
                return;
            }
            if (event.key === "Escape") {
                // Or the browser's own dialog-cancel closes the whole
                // explorer, which is a lot to lose for changing your mind.
                event.preventDefault();
                event.stopPropagation();
                edit.value = state.here;
                endEdit();
            }
        });
        edit.addEventListener("blur", () => {
            if (state.editing && state.crumbs) {
                edit.value = state.here;
                endEdit();
            }
        });

        setError("");
        setNote("");
        setHistory({});
        setPinned(false, false);

        return {
            element,
            messages,
            editBox: edit,
            address,
            setLocation,
            setHistory,
            setPinned,
            setError,
            setNote,
            beginEdit,
            endEdit,
            isEditing: () => state.editing,
            relayout() { if (!state.editing) fit(); },
        };
    }

    return { create };
})();
