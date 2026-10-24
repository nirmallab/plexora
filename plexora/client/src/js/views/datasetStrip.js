/**
 * datasetStrip.js - every sample in the dataset, one click away.
 *
 * Previous and Next (datasetNav.js) walk a dataset one sample at a time, which
 * is right for looking at the next one and wrong for reaching the fortieth.
 * Pressing the "2 / 12" counter drops this grid of thumbnails under the chip;
 * pressing a thumbnail goes there through the SAME go() the chevrons use, so
 * what travels with the move (services/carryOver.js) is exactly what travels
 * with Next. This file only picks; it never navigates or persists anything.
 *
 * THE SHAPE. Exactly the chip's width, key caps and all, so it reads as the
 * chip opening downward: a thin search row, then one thumbnail per row,
 * stacked for as far as the canvas has room, and any further columns scroll
 * sideways.
 *
 * THE SEARCH only narrows what is shown: every word typed must appear in the
 * sample's name or its title, and the grid refits to what is left. Focus
 * still opens on the current tile, so B and N keep walking with the strip
 * open; the field is a click, a Shift+Tab or an ArrowUp from the first tile
 * away. In it, ArrowDown goes to the first match and Enter picks it; Escape
 * clears a search before it closes the strip.
 *
 * WHERE IT MAY GO is measured, not assumed: fit() is plain arithmetic over the
 * boxes of whatever else sits on the canvas -- the sample's caption and the
 * expand button top-left, the channel legend and scale float bottom-right, the
 * overview lens bottom-left, and any plugin chrome marked
 * `data-viewer-furniture` (core names no plugin class). It re-measures while
 * open, because the legend grows as channels go on and the sidebar collapses
 * without an event.
 *
 * It lives in #openseadragon_wrapper, not a portal: fullscreen fullscreens the
 * whole page, so a wrapper child stays visible, and OpenSeadragon listens on
 * #openseadragon alone, so a press here never pans the image.
 */
window.PlexoraDatasetStrip = (function () {
    "use strict";


    //: Pixels. The strip is exactly as wide as the chip it hangs from, key
    //: caps and all, and shows COLUMNS thumbnails abreast; the tiles are
    //: sized from the chip's measured width, so they are set inline, not in
    //: the stylesheet.
    const GAP = 4;          // chip to strip, and clearance from any obstacle
    const MARGIN = 12;      // from the canvas edge, as the chip itself sits
    const PAD = 4;
    const BORDER = 1;
    const CELL_GAP = 4;
    const COLUMNS = 1;
    const SCROLLBAR = 10;   // the thin horizontal scrollbar, as Chrome draws it
    //: The search row over the grid, and the gap under it. Both repeated in
    //: viewer.css (.dataset-nav-strip-search); change both.
    const SEARCH = 22;
    const HEAD = SEARCH + CELL_GAP;

    //: Core's own canvas furniture, by id. Plugin chrome opts in by attribute.
    const FURNITURE_IDS = [
        "viewer_canvas_caption", "viewer_project_label", "sidebar_expand_button",
        "viewer_channel_legend", "viewer_mini_map",
    ];

    let state = null;

    const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

    /** The tile that fills COLUMNS abreast inside `width`, at 4:3 -- the
     *  shape of a project thumbnail. */
    function tileFor(width) {
        const w = Math.max(24, Math.floor(
            (width - 2 * PAD - 2 * BORDER - (COLUMNS - 1) * CELL_GAP) / COLUMNS));
        return { width: w, height: Math.round(w * 3 / 4) };
    }

    /** How many rows fit in `room` px of height. Up to COLUMNS columns the
     *  rows come out even (no full column beside a stub); past that every
     *  column is full and the rest scroll sideways, paying for the
     *  scrollbar's gutter. */
    function shape(room, count, cell) {
        const rowsIn = (height) => clamp(Math.floor(
            (height - 2 * PAD - 2 * BORDER - HEAD + CELL_GAP) / (cell.height + CELL_GAP)), 1, count);
        let rows = rowsIn(room);
        const columns = Math.ceil(count / rows);
        const scrolls = columns > COLUMNS;
        if (scrolls) rows = rowsIn(room - SCROLLBAR);
        else rows = Math.ceil(count / Math.min(COLUMNS, count));
        const height = HEAD + rows * (cell.height + CELL_GAP) - CELL_GAP + 2 * PAD + 2 * BORDER
            + (scrolls ? SCROLLBAR : 0);
        return { rows, height };
    }

    /**
     * Where the strip goes. Pure: every box is {top, right, bottom, left} in
     * the wrapper's coordinates, `host` is the wrapper's {width, height}.
     *
     * Hangs under the chip at the chip's own width. A box on the right that
     * starts above that line and reaches below it (a plugin dock) moves it
     * left, clear of it; one that starts below it (the legend, the lens)
     * caps the height -- but only if it lies under the strip, so a lens the
     * strip never reaches costs it no rows. "That line" is the foot of the
     * search row and the first row of tiles, the least the strip can be.
     */
    function fit({ host, anchor, obstacles = [], count }) {
        const top = anchor.bottom + GAP;
        const width = anchor.right - anchor.left;
        const cell = tileFor(width);
        const least = top + HEAD + cell.height;
        let rightLimit = anchor.right;
        for (const box of obstacles) {
            // Beside the strip rather than under it: anything already level
            // with its search or first row (a plugin dock stacked under the chip).
            if (!(box.top < least && box.bottom > top)) continue;
            if ((box.left + box.right) / 2 >= host.width / 2) {
                rightLimit = Math.min(rightLimit, box.left - GAP);
            }
        }
        const left = rightLimit - width;
        const n = Math.max(1, count || 0);
        const below = obstacles.filter((box) => box.top >= least
            && box.left < rightLimit && box.right > left);

        let floor = host.height - MARGIN;
        let result = shape(floor - top, n, cell);
        // Rows only shrink from pass to pass, so this settles; four passes is
        // more obstacles than the canvas has.
        for (let pass = 0; pass < 4; pass += 1) {
            const hits = below.filter((box) => box.top - GAP < top + result.height);
            if (!hits.length) break;
            floor = Math.min(floor, Math.min(...hits.map((box) => box.top)) - GAP);
            result = shape(floor - top, n, cell);
        }
        return { top, right: host.width - rightLimit, width, tile: cell, rows: result.rows };
    }

    function relative(rect, origin) {
        return {
            top: rect.top - origin.top, bottom: rect.bottom - origin.top,
            left: rect.left - origin.left, right: rect.right - origin.left,
        };
    }

    function inside(outer, node) {
        return !!(outer && node && outer.contains && outer.contains(node));
    }

    /** The furniture elements on the canvas right now, chip and strip aside. */
    function furniture(host, chip, root) {
        const found = [];
        for (const id of FURNITURE_IDS) {
            let el = document.getElementById(id);
            // The overview lens keeps the expanded map's whole box while only
            // its round button shows; opening it is a press, which closes this.
            if (el && id === "viewer_mini_map"
                && String(el.className).indexOf("is-expanded") === -1 && el.querySelector) {
                el = el.querySelector(".viewer-mini-map-lens") || el;
            }
            if (el) found.push(el);
        }
        const marked = host.querySelectorAll ? host.querySelectorAll("[data-viewer-furniture]") : [];
        for (const el of marked || []) found.push(el);
        const float = host.querySelector ? host.querySelector(".scale-float") : null;
        if (float) found.push(float);
        return found.filter((el) => el !== chip && el !== root
            && !inside(chip, el) && !inside(root, el));
    }

    /** Their boxes, in wrapper coordinates; a hidden one (no box) is not there. */
    function obstacles(host, chip, root) {
        const origin = host.getBoundingClientRect();
        const boxes = [];
        for (const el of furniture(host, chip, root)) {
            const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
            if (!rect || !(rect.width * rect.height > 0)) continue;
            boxes.push(relative(rect, origin));
        }
        return boxes;
    }


    function refit() {
        if (!state) return;
        const { host, chip, root, grid, tiles } = state;
        const box = host.getBoundingClientRect();
        const placed = fit({
            host: { width: box.width, height: box.height },
            anchor: relative(chip.getBoundingClientRect(), box),
            obstacles: obstacles(host, chip, root),
            count: tiles.length,
        });
        state.rows = placed.rows;
        root.style.top = placed.top + "px";
        root.style.right = placed.right + "px";
        root.style.width = placed.width + "px";
        grid.style.gridAutoColumns = placed.tile.width + "px";
        grid.style.gridTemplateRows = "repeat(" + placed.rows + ", " + placed.tile.height + "px)";
    }

    function tile(name, isCurrent, onChoose, shown = name) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "dataset-nav-strip-item" + (isCurrent ? " is-current" : "");
        item.title = shown;
        item.setAttribute("data-sample", name);
        if (isCurrent) item.setAttribute("aria-current", "true");

        const image = document.createElement("img");
        image.alt = "";
        image.loading = "lazy";
        image.decoding = "async";
        image.draggable = false;
        // A blank image has no thumbnail (the route answers 404), and a failed
        // one is an empty box that reads as a slow load forever. Say which.
        image.addEventListener("error", () => {
            if (item.className.indexOf("is-missing") !== -1) return;
            item.className += " is-missing";
            const fallback = document.createElement("span");
            fallback.className = "dataset-nav-strip-fallback";
            const icon = document.createElement("span");
            icon.className = "fas fa-image";
            icon.setAttribute("aria-hidden", "true");
            fallback.appendChild(icon);
            // Under the name, which stays drawn on top of it.
            item.insertBefore(fallback, label);
        });
        image.src = plexoraUrl("project_thumbnail/" + encodeURIComponent(name));
        item.appendChild(image);

        const label = document.createElement("span");
        label.className = "dataset-nav-strip-name";
        label.textContent = shown;
        item.appendChild(label);

        item.addEventListener("click", () => onChoose(name, isCurrent));
        return item;
    }

    // -- search -------------------------------------------------------------

    /** The words of a query, lower-cased; none for a blank one. */
    function terms(query) {
        return String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
    }

    /** Whether every word is in the tile's name or the title it shows. */
    function matches(item, words) {
        const text = (item.getAttribute("data-sample") + "\n" + item.title).toLowerCase();
        return words.every((word) => text.indexOf(word) !== -1);
    }

    /**
     * Show the tiles that match `query` and refit to them. Hidden by an
     * inline display rather than detached: moving a focused tile out of the
     * DOM and back would drop its focus.
     */
    function filter(query) {
        if (!state) return;
        const { all, grid, empty, search } = state;
        const words = terms(query);
        state.query = words.length ? String(query) : "";
        state.tiles = all.filter((item) => {
            const shown = matches(item, words);
            item.style.display = shown ? "" : "none";
            return shown;
        });
        const none = !state.tiles.length;
        grid.style.display = none ? "none" : "";
        empty.style.display = none ? "" : "none";
        search.className = "dataset-nav-strip-search" + (state.query ? " has-query" : "");
        grid.scrollLeft = 0;
        refit();
    }

    function onSearchInput() {
        if (state) filter(state.input.value);
    }

    function onSearchKey(event) {
        if (!state) return;
        const first = state.tiles[0];
        if (event.key === "ArrowDown") {
            event.preventDefault();
            event.stopPropagation();
            if (first && first.focus) first.focus();
        } else if (event.key === "Enter") {
            event.preventDefault();
            event.stopPropagation();
            // Through the tile's own choice, so the current sample only closes.
            if (first) state.choose(first.getAttribute("data-sample"),
                                    first.className.indexOf("is-current") !== -1);
        }
    }

    /** Back to every tile. Focus is left where it is (a tile is still a
     *  tile) unless `focusInput`, for the clear button, which has none. */
    function clearSearch(focusInput) {
        if (!state) return;
        state.input.value = "";
        filter("");
        if (focusInput && state.input.focus) state.input.focus();
    }

    function searchRow() {
        // A div rather than a <label>, which may not hold the clear button, so
        // a press on the glass is sent to the field by hand. Font Awesome
        // swaps the glass's span for an <svg>, so it is not named: anything
        // but the field and the clear button counts.
        const row = document.createElement("div");
        row.className = "dataset-nav-strip-search";
        row.addEventListener("click", (event) => {
            if (!state || event.target === state.input || inside(clear, event.target)) return;
            state.input.focus();
        });

        const icon = document.createElement("span");
        icon.className = "fas fa-magnifying-glass";
        icon.setAttribute("aria-hidden", "true");
        row.appendChild(icon);

        const input = document.createElement("input");
        input.type = "text";
        input.className = "dataset-nav-strip-input";
        input.placeholder = "Search samples";
        input.autocomplete = "off";
        input.spellcheck = false;
        input.setAttribute("aria-label", "Search samples");
        input.addEventListener("input", onSearchInput);
        input.addEventListener("keydown", onSearchKey);
        row.appendChild(input);

        const clear = document.createElement("button");
        clear.type = "button";
        clear.className = "dataset-nav-strip-clear";
        clear.title = "Clear search";
        clear.setAttribute("aria-label", "Clear search");
        clear.tabIndex = -1;
        const cross = document.createElement("span");
        cross.className = "fas fa-xmark";
        cross.setAttribute("aria-hidden", "true");
        clear.appendChild(cross);
        clear.addEventListener("click", () => clearSearch(true));
        row.appendChild(clear);

        return { row, input };
    }

    // -- dismissal ----------------------------------------------------------

    function onKeyDown(event) {
        // Everything but Escape goes on its way untouched: B, N, PageUp and
        // PageDown still walk with the strip open (go() closes it).
        if (!state || event.key !== "Escape") return;
        event.stopPropagation();
        if (event.preventDefault) event.preventDefault();
        // A search first: the first Escape takes back what was typed.
        if (state.query || state.input.value) { clearSearch(); return; }
        const anchor = state.anchor;
        close();
        if (anchor && anchor.focus) anchor.focus();
    }

    function onPointerDown(event) {
        if (!state) return;
        const target = event.target;
        // The counter is the toggle; leaving its press alone lets its own
        // click close the strip instead of closing it here and reopening it.
        if (inside(state.root, target) || inside(state.anchor, target)) return;
        close();
    }

    function onGridKey(event) {
        if (!state) return;
        const index = state.tiles.indexOf(event.target);
        if (index === -1) return;
        const last = state.tiles.length - 1;
        const rows = state.rows || 1;
        const step = {
            ArrowDown: index + 1, ArrowUp: index - 1,
            ArrowRight: index + rows, ArrowLeft: index - rows,
            Home: 0, End: last,
        }[event.key];
        if (step === undefined) return;
        event.preventDefault();
        event.stopPropagation();
        // Up off the top of the first column is the search.
        const next = step < 0 && event.key === "ArrowUp"
            ? state.input : state.tiles[clamp(step, 0, last)];
        if (next && next.focus) next.focus();
    }

    function onWheel(event) {
        if (!state) return;
        const grid = state.grid;
        // A mouse wheel only turns one way; sideways is the only way this
        // scrolls. A trackpad's own sideways swipe is left to the browser.
        if (!(grid.scrollWidth > grid.clientWidth) || !event.deltaY) return;
        if (Math.abs(event.deltaX || 0) > Math.abs(event.deltaY)) return;
        grid.scrollLeft += event.deltaY;
        event.preventDefault();
    }

    function onHidden() { close(); }

    // -- lifecycle ----------------------------------------------------------

    /**
     * @param anchor  the counter button: marked expanded, and given focus back.
     * @param chip    the box to hang under (the whole nav); defaults to anchor.
     * @param members sample names, in the dataset's own order.
     * @param current the sample on screen, marked and inert.
     * @param onPick  called with a name AFTER the strip has closed.
     * @param titles  name -> display title, for members that have one; a
     *                tile shows that and is still picked by its name.
     */
    function open({ anchor, chip, members, current, label, onPick, titles = {} }) {
        close();
        const host = document.getElementById("openseadragon_wrapper");
        if (!host || !anchor || !members || !members.length) return false;
        chip = chip || anchor;

        const root = document.createElement("div");
        root.className = "dataset-nav-strip";
        root.setAttribute("role", "group");
        root.setAttribute("aria-label", label ? "Samples in " + label : "Samples");

        const { row: search, input } = searchRow();
        root.appendChild(search);

        const grid = document.createElement("div");
        grid.className = "dataset-nav-strip-grid";
        root.appendChild(grid);

        const empty = document.createElement("div");
        empty.className = "dataset-nav-strip-empty";
        empty.textContent = "No sample matches";
        empty.style.display = "none";
        root.appendChild(empty);

        const choose = (name, isCurrent) => {
            close();
            if (!isCurrent && onPick) onPick(name);
        };
        const tiles = members.map((name) => tile(name, name === current, choose,
                                                   (titles && titles[name]) || name));
        for (const item of tiles) grid.appendChild(item);
        grid.addEventListener("keydown", onGridKey);
        grid.addEventListener("wheel", onWheel, { passive: false });

        host.appendChild(root);
        state = {
            host, anchor, chip, root, grid, empty, search, input,
            all: tiles, tiles: tiles.slice(), query: "", choose, rows: 1, observer: null,
        };
        anchor.setAttribute("aria-expanded", "true");
        refit();

        document.addEventListener("keydown", onKeyDown, true);
        document.addEventListener("pointerdown", onPointerDown, true);
        window.addEventListener("plexora:viewer-hidden", onHidden);
        window.addEventListener("resize", refit);
        if (typeof ResizeObserver === "function") {
            const observer = new ResizeObserver(() => refit());
            observer.observe(host);
            for (const el of furniture(host, chip, root)) observer.observe(el);
            state.observer = observer;
        }

        // The current sample in view and focused, so the arrows start there.
        // By hand rather than scrollIntoView, which may also scroll whatever
        // clips the canvas.
        const here = tiles.find((item) => item.className.indexOf("is-current") !== -1);
        if (here) {
            if (grid.scrollWidth > grid.clientWidth) {
                grid.scrollLeft = Math.max(0,
                    // Both offsets are from the frame, which is what is positioned.
                    here.offsetLeft - (grid.offsetLeft || 0)
                        - (grid.clientWidth - here.offsetWidth) / 2);
            }
            if (here.focus) here.focus({ preventScroll: true });
        }
        return true;
    }

    /** Idempotent: the DOM, the listeners and the observer all go. */
    function close() {
        if (!state) return;
        const { root, anchor, observer } = state;
        const hadFocus = inside(root, document.activeElement);
        state = null;
        document.removeEventListener("keydown", onKeyDown, true);
        document.removeEventListener("pointerdown", onPointerDown, true);
        window.removeEventListener("plexora:viewer-hidden", onHidden);
        window.removeEventListener("resize", refit);
        if (observer) observer.disconnect();
        if (root.parentNode) root.parentNode.removeChild(root);
        if (anchor) {
            anchor.setAttribute("aria-expanded", "false");
            if (hadFocus && anchor.focus) anchor.focus();
        }
    }

    return {
        open,
        close,
        isOpen: () => !!state,
        fit,
        //: Test seams.
        _obstacles: obstacles,
        _refit: refit,
        _sizes: { GAP, MARGIN, PAD, BORDER, CELL_GAP, COLUMNS, SCROLLBAR, SEARCH, HEAD },
        _tileFor: tileFor,
    };
})();
