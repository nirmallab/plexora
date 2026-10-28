/**
 * machineChip.js -- "which machine is this file on?", asked the way the home
 * page asks it.
 *
 * The home page settled the shape (see quickViewLanding.js): a compact chip at
 * the FRONT of the path field, reading "Local" or the server's name, whose
 * menu lists the actual machines -- Local, every saved server with whether it
 * is up, "Add a server…" and "Manage servers". The Import dialog copied it
 * (importSample.js). The form dialogs kept the older L | R switch with a
 * machine chip beside it, which asked the same question in a second shape,
 * and with a step more: Remote first, then which.
 *
 * This is that chip for every field that is not the home page or the Import
 * dialog: the requirements form, the project edit page, the channel-names and
 * gene-group dialogs. It draws and asks; it holds no state of its own. The
 * caller says where the field is now (`current`) and is told when somebody
 * picks somewhere else (`onPick`) -- the field's own code is what knows what a
 * path on that machine submits as, and that has not moved.
 *
 * The classes are the Import dialog's, deliberately: `.plx-import-machine` and
 * `.plx-import-menu-*` are already styled, and the same chip in two dialogs
 * should not be two stylesheets that drift.
 *
 * A machine that is saved but not up is listed, and picking it opens the
 * connection modal for it -- as on the home page -- rather than putting the
 * field on a filesystem nothing can read yet.
 *
 * Served as a classic script (see base.html).
 */
window.PlexoraMachineChip = (function () {
    "use strict";

    //: The two answers, spelled as dataLocation.js spells them.
    const LOCAL = "local";
    const REMOTE = "remote";

    //: The chip whose menu is open. One at a time: a form with two fields has
    //: two chips, and opening the second puts the first one's list away.
    let shown = null;

    const Remotes = () => window.PlexoraRemotes;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    /** Whether a file could be named on this machine now. A files-only
     *  server has no node; its SFTP connection is the answer. */
    function connected(place) {
        return place.kind === "server" || Boolean(place.node) || Boolean(place.files);
    }

    function asPlace(place) {
        return { id: place.id, kind: place.kind, label: place.label,
                 node: place.node || null, files: place.files || null };
    }

    /**
     * @param options.current - `() => ({where, place})`, read on every paint:
     *   the field's own state, never a copy of it.
     * @param options.onPick  - `(where, place)` when somebody picks Local or a
     *   connected machine. The caller moves its field; the chip repaints after.
     * @param options.what    - what the field holds, for the connection
     *   modal's sentence ("the mask", "the marker list"). Default "the file".
     * @returns `{element, paint, close, isOpen}`
     */
    function create(options = {}) {
        const current = options.current || (() => ({ where: LOCAL, place: null }));
        const what = options.what || "the file";

        const button = el("button", "plx-import-machine plx-machine-chip");
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
        const label = button.querySelector(".plx-import-machine-label");

        const menu = el("div", "plx-import-menu plx-machine-menu");
        menu.setAttribute("role", "menu");
        menu.setAttribute("aria-label", "Where the file is");
        menu.hidden = true;

        let unwatch = null;
        //: The list drawn last, so a poll that changes nothing does not rebuild
        //: the menu under the keyboard focus.
        let drawn = "";

        function paint() {
            const now = current();
            const away = now.where === REMOTE;
            const name = away
                ? ((now.place && (now.place.label || now.place.id)) || "Remote")
                : "Local";
            button.classList.toggle("is-remote", away);
            if (label) label.textContent = name;
            button.setAttribute("aria-label", away
                ? `On ${name}. Change machine`
                : "On this computer. Change machine");
            button.title = away ? `On ${name}` : "On this computer";
        }

        function isOpen() {
            return !menu.hidden;
        }

        function items() {
            return Array.from(menu.querySelectorAll(".plx-import-menu-item:not(:disabled)"));
        }

        function item({ icon, dot, name, sub, checked, onPick, href, role, disabled, add }) {
            const row = el(href ? "a" : "button", "plx-import-menu-item" + (add ? " is-add" : ""));
            row.setAttribute("role", role || "menuitem");
            if (role === "menuitemradio") row.setAttribute("aria-checked", checked ? "true" : "false");
            if (href) {
                row.href = href;
            } else {
                row.type = "button";
                row.disabled = Boolean(disabled);
                row.addEventListener("click", onPick);
            }
            const lead = el("span", dot || "plx-import-menu-icon");
            lead.setAttribute("aria-hidden", "true");
            if (!dot) lead.appendChild(el("span", `fas fa-${icon}`));
            row.append(lead, el("span", "plx-import-menu-name", name));
            if (sub) row.append(el("span", "plx-import-menu-sub", sub));
            if (checked) {
                const tick = el("span", "plx-import-menu-check");
                tick.setAttribute("aria-hidden", "true");
                tick.appendChild(el("span", "fas fa-check"));
                row.append(tick);
            }
            return row;
        }

        function draw(snapshot) {
            const places = (snapshot && snapshot.places) || [];
            const now = current();
            const chosen = now.where === REMOTE && now.place ? now.place.id : null;
            const key = JSON.stringify([chosen, Boolean(snapshot && snapshot.loaded),
                places.map((place) => [place.id, place.node, place.state, place.phase])]);
            if (key === drawn) return;
            drawn = key;
            const focused = menu.contains(document.activeElement)
                ? document.activeElement.dataset.place : null;
            const sep = () => {
                const line = el("div", "plx-import-menu-sep");
                line.setAttribute("role", "separator");
                return line;
            };
            const rows = [item({
                icon: "laptop", name: "Local", sub: "This computer", role: "menuitemradio",
                checked: now.where !== REMOTE, onPick: () => pick(LOCAL, null),
            }), sep(), el("div", "plx-import-menu-label", "Remote")];
            if (!places.length) {
                // Before the first answer the list is unknown, not empty.
                rows.push(el("div", "plx-import-menu-empty",
                             snapshot && snapshot.loaded ? "No servers yet." : "Looking for servers…"));
            }
            places.forEach((place) => {
                const up = connected(place);
                const waiting = !up && Remotes() && Remotes().isOpening(place.state);
                const row = item({
                    dot: "plx-import-menu-dot" + (up ? "" : (waiting ? " is-waiting" : " is-off")),
                    name: place.label,
                    sub: up ? "Connected" : (waiting ? (place.phase || "Connecting…") : "Not connected"),
                    role: "menuitemradio",
                    checked: place.id === chosen,
                    disabled: waiting,
                    onPick: () => (up ? pick(REMOTE, asPlace(place)) : connect(place)),
                });
                row.dataset.place = place.id;
                rows.push(row);
            });
            rows.push(sep());
            rows.push(item({ icon: "plus", name: "Add a server…", add: true,
                             onPick: () => connect(null) }));
            rows.push(item({ icon: "gear", name: "Manage servers", sub: "Settings",
                             href: plexoraUrl("settings") + "#remotes" }));
            menu.replaceChildren(...rows);
            if (focused) menu.querySelector(`[data-place="${CSS.escape(focused)}"]`)?.focus();
        }

        /**
         * Under the chip, fixed, and inside the dialog the chip is in when it
         * is in one: a modal dialog is the top layer, so a menu under <body>
         * would open behind it. Fixed rather than in the field, because a
         * dialog's body scrolls and would clip it. Turned up over the chip
         * when there is no room below, and kept off the right edge.
         */
        function position() {
            const rect = button.getBoundingClientRect();
            const wide = menu.offsetWidth;
            const tall = menu.offsetHeight;
            const left = Math.max(8, Math.min(rect.left - 2, window.innerWidth - wide - 8));
            const below = rect.bottom + 6;
            const top = below + tall > window.innerHeight - 8 && rect.top - 6 - tall > 8
                ? rect.top - 6 - tall : below;
            menu.style.left = `${Math.round(left)}px`;
            menu.style.top = `${Math.round(top)}px`;
        }

        function openMenu() {
            if (isOpen() || button.disabled) return;
            if (shown && shown !== handle) shown.close(false);
            shown = handle;
            (button.closest("dialog") || document.body).appendChild(menu);
            drawn = "";
            draw(Remotes() ? Remotes().snapshot() : null);
            menu.hidden = false;
            position();
            button.setAttribute("aria-expanded", "true");
            // `active`: the list is on screen, so a machine that comes up while
            // it is open -- connected from Settings, or by another tab -- shows.
            if (Remotes()) {
                unwatch = Remotes().subscribe((snapshot) => draw(snapshot), { active: true });
            }
            document.addEventListener("pointerdown", outside, true);
            document.addEventListener("scroll", scrolled, true);
            window.addEventListener("resize", dismiss);
            // Without scrolling: a scroll is what puts this menu away.
            items()[0]?.focus({ preventScroll: true });
        }

        function close(refocus) {
            if (!isOpen()) return;
            menu.hidden = true;
            menu.remove();
            button.setAttribute("aria-expanded", "false");
            if (unwatch) unwatch();
            unwatch = null;
            if (shown === handle) shown = null;
            document.removeEventListener("pointerdown", outside, true);
            document.removeEventListener("scroll", scrolled, true);
            window.removeEventListener("resize", dismiss);
            if (refocus) button.focus();
        }

        function dismiss() {
            close(false);
        }

        // A press anywhere else puts the menu away, as every menu in the
        // navbar does.
        function outside(event) {
            if (!menu.contains(event.target) && !button.contains(event.target)) close(false);
        }

        // The menu is fixed and the chip is not: a dialog body scrolled under
        // it would leave the list pointing at nothing.
        function scrolled(event) {
            if (!menu.contains(event.target)) close(false);
        }

        function pick(where, chosen) {
            close(false);
            if (options.onPick) options.onPick(where, chosen);
            paint();
        }

        /**
         * Connect a saved server that is not up yet, or add a new one, through
         * the connection modal -- the surface built for passwords and progress
         * -- and use it the moment it is. Cancelling leaves the field as it was.
         */
        async function connect(saved) {
            close(false);
            if (!window.PlexoraConnectionModal) return;
            const outcome = await window.PlexoraConnectionModal.open(saved
                ? { name: saved.id, kind: "node",
                    intent: saved.files_only
                        ? `Opening an SFTP connection to this machine, so ${what} can be `
                          + "read from it. Nothing is started there."
                        : `Opening a data node on this machine, so ${what} can be read `
                          + "from it." }
                : { kind: "node",
                    intent: `Add the machine ${what} is on. Plexora will handle the SSH `
                            + "connection." });
            if (outcome && outcome.connected) {
                pick(REMOTE, { id: outcome.name, kind: "remote",
                               label: outcome.label || (saved && saved.label) || outcome.name,
                               node: outcome.node || null, files: outcome.files || null });
            } else {
                button.focus();
            }
        }

        button.addEventListener("click", () => {
            if (isOpen()) close(true);
            else openMenu();
        });
        button.addEventListener("keydown", (event) => {
            if (event.key === "ArrowDown" && !isOpen()) {
                event.preventDefault();
                openMenu();
            }
        });
        menu.addEventListener("keydown", (event) => {
            const list = items();
            const at = list.indexOf(document.activeElement);
            if (event.key === "Escape") {
                // The dialog's own Escape would close the whole dialog.
                event.preventDefault();
                event.stopPropagation();
                close(true);
            } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                const step = event.key === "ArrowDown" ? 1 : -1;
                list[(at + step + list.length) % list.length]?.focus();
            } else if (event.key === "Home" || event.key === "End") {
                event.preventDefault();
                list[event.key === "Home" ? 0 : list.length - 1]?.focus();
            } else if (event.key === "Tab") {
                close(false);
            }
        });

        const handle = { element: button, paint, close, isOpen };
        paint();
        return handle;
    }

    /**
     * Put the chip INSIDE the path box, at its front, as the home page does.
     * "Which machine?" and "which file?" are one question asked in two
     * halves, and a control beside the box read as a setting of its own.
     *
     * The chip floats over the input's left padding rather than the input
     * moving into a box drawn to look like one: every dialog styles its
     * `.form-control` its own way -- height, ground, focus ring, the red of a
     * path that is not there -- and the input keeps all of it. The padding
     * follows the chip's width, which changes with the machine's name.
     *
     * @returns the box, now where the input was.
     */
    function wrap(input, chip) {
        const box = el("div", "plx-machine-field");
        input.parentNode.insertBefore(box, input);
        box.append(chip.element, input);
        if (typeof ResizeObserver === "function") {
            new ResizeObserver(() => {
                const wide = chip.element.offsetWidth;
                if (wide) box.style.setProperty("--plx-machine-chip-width", `${wide}px`);
            }).observe(chip.element);
        }
        return box;
    }

    return { create, wrap, LOCAL, REMOTE };
})();
