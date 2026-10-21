/**
 * explorerSidebar.js -- the explorer's places: Favorites (Home, the standard
 * folders, the user's pins), Locations (this machine's drives and volumes,
 * then project storage) and Recent.
 *
 * Every place is a button carrying the whole path the server gave it; the
 * label is display only. What the volumes are called comes from the server
 * (/fs_places): "Data (D:)" on Windows, the volume name under /Volumes on a
 * Mac, the mount point's name on Linux and HPC -- nothing is hardcoded here,
 * so a cluster's /n/scratch and /n/groups show up because they are mounted,
 * not because this file knows the cluster.
 *
 * Pins reorder by dragging their grip (SortableJS, when the page has it); the
 * new order is handed back for the server to keep. Collapsed sections are
 * remembered with the rest of the view.
 */
window.PlexoraExplorerSidebar = (function () {
    "use strict";

    const T = () => window.PlexoraExplorerTypes;

    const SECTIONS = [
        ["favorites", "Favorites"],
        ["locations", "Locations"],
        ["recent", "Recent"],
    ];

    const KNOWN_ICONS = {
        desktop: "fas fa-desktop",
        documents: "fas fa-file-lines",
        downloads: "fas fa-download",
    };

    const VOLUME_ICONS = {
        local: "fas fa-hard-drive",
        removable: "fas fa-plug",
        network: "fas fa-network-wired",
        remote: "fas fa-cloud",
    };

    function create({ onGo = () => {}, onReorderPins = () => {}, onContextMenu = () => {},
                      onCollapse = () => {} } = {}) {
        const { el, button, glyph, setIcon, readableSize, shortName } = T();

        const state = {
            home: "",
            known: [],
            volumes: [],
            project: [],
            unavailable: "",
            pinned: [],
            recent: [],
            current: "",
            collapsed: new Set(),
            sortable: null,
        };

        const element = el("aside", "plx-explorer-sidebar");
        element.setAttribute("aria-label", "Places");

        const machine = el("div", "plx-explorer-machine");
        const machineGlyph = glyph("fas fa-computer", "plx-explorer-machine-icon");
        const machineName = el("span", "plx-explorer-machine-name", "This computer");
        machine.append(machineGlyph, machineName);
        element.append(machine);

        const bodies = {};
        const heads = {};
        SECTIONS.forEach(([key, label]) => {
            const section = el("section", "plx-explorer-section");
            section.setAttribute("data-section", key);
            const head = button("plx-explorer-section-head", "", `${label}`);
            head.setAttribute("aria-expanded", "true");
            const chevron = glyph("fas fa-chevron-down", "plx-explorer-section-chevron");
            head.append(chevron, el("span", "plx-explorer-section-label", label));
            head.addEventListener("click", () => {
                if (state.collapsed.has(key)) state.collapsed.delete(key);
                else state.collapsed.add(key);
                paintCollapsed();
                onCollapse(Array.from(state.collapsed));
            });
            const sectionBody = el("div", "plx-explorer-section-body");
            section.append(head, sectionBody);
            element.append(section);
            bodies[key] = sectionBody;
            heads[key] = { head, chevron, section };
        });

        const pinSlot = el("div", "plx-explorer-pins");

        function paintCollapsed() {
            SECTIONS.forEach(([key]) => {
                const closed = state.collapsed.has(key);
                const { head, chevron, section } = heads[key];
                head.setAttribute("aria-expanded", closed ? "false" : "true");
                setIcon(chevron, closed ? "fas fa-chevron-right" : "fas fa-chevron-down");
                bodies[key].hidden = closed;
                section.classList.toggle("is-collapsed", closed);
            });
        }

        // -- one place -------------------------------------------------------

        function place({ label, path, icon, caption = "", title = "", kind = "place", go = null }) {
            const node = button("plx-explorer-place", "", "");
            node.setAttribute("data-path", path);
            node.setAttribute("title", title || path || label);
            node.append(glyph(icon, "plx-explorer-place-icon"));
            const text = el("span", "plx-explorer-place-text");
            text.append(el("span", "plx-explorer-place-label", label));
            if (caption) text.append(el("span", "plx-explorer-place-caption", caption));
            node.append(text);
            node.addEventListener("click", () => onGo(go === null ? path : go));
            node.addEventListener("contextmenu", (event) => {
                if (event.preventDefault) event.preventDefault();
                onContextMenu({ kind, path, label }, { x: event.clientX || 0, y: event.clientY || 0 });
            });
            return node;
        }

        function usage(volume) {
            const total = Number(volume.total);
            const free = Number(volume.free);
            if (!Number.isFinite(total) || !Number.isFinite(free) || total <= 0) return "";
            return `${readableSize(free)} free of ${readableSize(total)}`;
        }

        // -- the sections ----------------------------------------------------

        function renderFavorites() {
            const body = bodies.favorites;
            body.replaceChildren();
            // Home goes by "" -- the server's word for its user's home -- so it
            // works before /fs_places has said where that is.
            body.append(place({ label: "Home", path: state.home, go: "", icon: "fas fa-house",
                                title: state.home || "Your home folder" }));
            state.known.forEach((entry) => {
                body.append(place({ label: entry.label, path: entry.path,
                                    icon: KNOWN_ICONS[entry.id] || "fas fa-folder" }));
            });
            renderPins();
            body.append(pinSlot);
        }

        function renderPins() {
            pinSlot.replaceChildren();
            state.pinned.forEach((path) => {
                const row = el("div", "plx-explorer-pinrow is-pin");
                row.setAttribute("data-path", path);
                const node = place({ label: shortName(path), path, icon: "fas fa-folder",
                                     kind: "pin" });
                node.classList.add("is-pin");
                const grip = glyph("fas fa-grip-vertical", "plx-explorer-grip");
                grip.setAttribute("title", "Drag to reorder");
                row.append(node, grip);
                pinSlot.append(row);
            });
            ensureSortable();
        }

        function ensureSortable() {
            if (state.sortable || !state.pinned.length) return;
            const cards = window.PlexoraCardList;
            if (!cards || typeof cards.ensureSortable !== "function") return;
            state.sortable = cards.ensureSortable(pinSlot, {
                handle: ".plx-explorer-grip",
                draggable: ".is-pin",
                onSort: () => {
                    const order = Array.from(pinSlot.children || [])
                        .map((row) => row.getAttribute("data-path"))
                        .filter(Boolean);
                    state.pinned = order;
                    onReorderPins(order);
                },
            });
        }

        function renderLocations() {
            const body = bodies.locations;
            body.replaceChildren();
            state.volumes.forEach((volume) => {
                const details = [volume.fstype, volume.source].filter(Boolean).join(" · ");
                body.append(place({
                    label: volume.label || volume.path,
                    path: volume.path,
                    icon: VOLUME_ICONS[volume.kind] || VOLUME_ICONS.local,
                    caption: usage(volume),
                    title: details ? `${volume.path} (${details})` : volume.path,
                    kind: "volume",
                }));
            });
            state.project.forEach((entry) => {
                body.append(place({
                    label: entry.label,
                    path: entry.path,
                    icon: /^plexora data$/i.test(entry.label) ? "fas fa-folder-tree" : "fas fa-folder",
                    kind: "project",
                }));
            });
            if (state.unavailable) {
                body.append(el("div", "plx-explorer-place-note", state.unavailable));
            }
            heads.locations.section.hidden = !state.volumes.length && !state.project.length
                && !state.unavailable;
        }

        function renderRecent() {
            const body = bodies.recent;
            body.replaceChildren();
            state.recent.forEach((path) => {
                body.append(place({ label: shortName(path), path, icon: "fas fa-clock-rotate-left",
                                    kind: "recent" }));
            });
            heads.recent.section.hidden = !state.recent.length;
        }

        function paintCurrent() {
            const nodes = [];
            (function walk(node) {
                (node.children ? Array.from(node.children) : []).forEach((child) => {
                    if (child.classList && child.classList.contains("plx-explorer-place")) nodes.push(child);
                    walk(child);
                });
            })(element);
            nodes.forEach((node) => {
                const on = Boolean(state.current) && node.getAttribute("data-path") === state.current;
                node.classList.toggle("is-current", on);
                if (on) node.setAttribute("aria-current", "location");
                else node.removeAttribute("aria-current");
            });
        }

        function renderAll() {
            renderFavorites();
            renderLocations();
            renderRecent();
            paintCurrent();
        }

        // Home and the section heads before /fs_places has answered -- or
        // when it never does.
        renderAll();

        // -- what the controller tells it ---------------------------------------

        return {
            element,
            setMachine({ label = "", icon = "fas fa-computer", title = "" } = {}) {
                machineName.textContent = label;
                machine.setAttribute("title", title || label);
                setIcon(machineGlyph, icon);
            },
            setPlaces({ home = "", known = [], volumes = [], project = [], unavailable = "" } = {}) {
                state.home = home || "";
                state.known = Array.isArray(known) ? known : [];
                state.volumes = Array.isArray(volumes) ? volumes : [];
                state.project = Array.isArray(project) ? project : [];
                state.unavailable = unavailable || "";
                renderAll();
            },
            setPinned(paths) {
                state.pinned = Array.isArray(paths) ? paths.slice() : [];
                renderPins();
                paintCurrent();
            },
            setRecent(paths) {
                state.recent = Array.isArray(paths) ? paths.slice() : [];
                renderRecent();
                paintCurrent();
            },
            setCurrent(here) {
                state.current = here || "";
                paintCurrent();
            },
            setCollapsed(list) {
                state.collapsed = new Set((Array.isArray(list) ? list : [])
                    .filter((key) => SECTIONS.some(([name]) => name === key)));
                paintCollapsed();
            },
            render: renderAll,
        };
    }

    return { create };
})();
