/**
 * explorerListing.js -- one directory's entries as a sortable, virtualized
 * list (or a grid of tiles), with the selection rules a field asked for.
 *
 * Virtualized because a scratch directory holds fifty thousand files and a
 * row element each is a page that cannot scroll. Only the rows in view (plus
 * a few either side) exist; a spacer the height of all of them gives the
 * scrollbar its true length. Focus stays on the scrolling container and
 * `aria-activedescendant` names the cursor row, which is always one of the
 * rendered ones -- moving the cursor scrolls by arithmetic, never by
 * `scrollIntoView` on a row that may not exist.
 *
 * Clicks are delegated to the container (`closest(".plx-explorer-row")` +
 * `data-index`): rows come and go on every scroll, and a listener per row
 * would be rebuilt with them.
 *
 * Sorting is client-side over what /list_dir sent. A partial listing (no size
 * or date yet for most rows) asked to sort by size or date sorts by name until
 * the metadata chunks land, with a spinner in that column's header, rather
 * than reshuffling under the pointer as each chunk arrives.
 *
 * Paths are never built here: `pathOf(entry)` is the controller's, and it
 * answers with the server's `entry.path`.
 */
window.PlexoraExplorerListing = (function () {
    "use strict";

    const T = () => window.PlexoraExplorerTypes;

    const ROW_H = 28;
    const OVERSCAN = 8;
    const TILE_W = 112;
    const TILE_H = 96;
    //: How many rows to draw when the container has not been laid out (a
    //: hidden window, the probe's DOM).
    const FALLBACK_ROWS = 40;

    const COLUMN_DEFAULTS = { mtime: 150, kind: 120, size: 80 };
    const COLUMN_MIN = 60;
    const COLUMN_MAX = 600;
    //: The direction a column sorts in when first clicked: newest and largest
    //: first, names and types A to Z.
    const FIRST_DIRECTION = { name: "asc", mtime: "desc", kind: "asc", size: "desc" };
    const COLUMN_LABELS = { name: "Name", where: "Where", mtime: "Date Modified",
                            kind: "Type", size: "Size" };

    const DEFAULT_VIEW = {
        sort_by: "name",
        sort_dir: "asc",
        dirs_first: true,
        layout: "list",
        columns: { ...COLUMN_DEFAULTS },
    };

    /**
     * A name as something `<` sorts naturally: lower case, and every run of
     * digits padded so `f10` follows `f9`. Far cheaper than a collator call
     * per comparison, which over fifty thousand names is the difference
     * between a click and a stall.
     */
    function naturalKey(name) {
        return String(name || "").toLowerCase()
            .replace(/\d+/g, (digits) => digits.replace(/^0+(?=\d)/, "").padStart(16, "0"));
    }

    function compareKeys(a, b) {
        return a < b ? -1 : a > b ? 1 : 0;
    }

    function create({ domId = "plx-explorer", accept, pathOf,
                      onOpen = () => {}, onActivate = () => {}, onSelection = () => {},
                      onContextMenu = () => {}, onSortChange = () => {},
                      onColumnsChange = () => {}, onExplain = () => {} } = {}) {
        const { el, button, glyph, setIcon, usable, typeLabel, iconFor,
                readableSize, readableDate, storeOf } = T();

        const state = {
            accept,
            entries: [],
            partial: false,
            meta: new Map(),
            metaDone: true,
            query: "",
            deep: null,
            view: { ...DEFAULT_VIEW, columns: { ...COLUMN_DEFAULTS } },
            sorted: null,
            sortPending: false,
            rows: [],
            //: `version` says the drawn rows are stale; `dataVersion` says
            //: the sort is. A chunk of sizes redraws rows without re-sorting.
            version: 0,
            dataVersion: 0,
            rendered: { first: -1, last: -1, version: -1, layout: "", cols: 0 },
            selected: new Set(),
            anchor: -1,
            cursor: -1,
            cursorPath: null,
            busy: false,
            emptyText: "",
            resizing: null,
            frame: false,
        };

        // -- the elements ------------------------------------------------------

        const root = el("div", "plx-explorer-listing");
        const head = el("div", "plx-explorer-head");
        head.setAttribute("role", "row");
        const scroller = el("div", "plx-explorer-scroller");
        scroller.setAttribute("role", "listbox");
        scroller.setAttribute("tabindex", "0");
        scroller.setAttribute("aria-label", "Folder contents");
        const spacer = el("div", "plx-explorer-spacer");
        const block = el("div", "plx-explorer-rows");
        block.setAttribute("role", "presentation");
        spacer.append(block);
        scroller.append(spacer);
        const overlay = el("div", "plx-explorer-overlay");
        overlay.setAttribute("aria-live", "polite");
        overlay.hidden = true;
        root.append(head, scroller, overlay);

        function paintColumns() {
            const columns = state.view.columns || COLUMN_DEFAULTS;
            ["mtime", "kind", "size"].forEach((name) => {
                const width = Number(columns[name]) || COLUMN_DEFAULTS[name];
                if (root.style && root.style.setProperty) {
                    root.style.setProperty(`--plx-col-${name}`, `${width}px`);
                }
            });
        }

        // -- the header ------------------------------------------------------

        function headerColumns() {
            return state.deep ? ["name", "where", "kind"] : ["name", "mtime", "kind", "size"];
        }

        function renderHead() {
            head.replaceChildren();
            const sortBy = state.view.sort_by;
            headerColumns().forEach((name) => {
                const cell = el("div", `plx-explorer-headcell is-${name}`);
                cell.setAttribute("role", "columnheader");
                cell.setAttribute("data-col", name);
                const sortable = !state.deep && name !== "where";
                const on = sortable && sortBy === name;
                cell.setAttribute("aria-sort", on
                    ? (state.view.sort_dir === "desc" ? "descending" : "ascending") : "none");
                const control = sortable
                    ? button("plx-explorer-col", "", `Sort by ${COLUMN_LABELS[name]}`)
                    : el("span", "plx-explorer-col is-static");
                control.setAttribute("data-col", name);
                control.append(el("span", "plx-explorer-col-label", COLUMN_LABELS[name]));
                if (on) {
                    const caret = glyph(state.sortPending
                        ? "fas fa-circle-notch"
                        : state.view.sort_dir === "desc" ? "fas fa-caret-down" : "fas fa-caret-up",
                        "plx-explorer-caret");
                    if (state.sortPending) {
                        caret.classList.add("is-spinning");
                        cell.setAttribute("title", "Sorting once sizes and dates have been read");
                    }
                    control.append(caret);
                }
                if (sortable) control.addEventListener("click", () => sortOn(name));
                cell.append(control);
                if (name === "mtime" || name === "kind" || name === "size") {
                    const divider = el("span", "plx-explorer-divider");
                    divider.setAttribute("data-col", name);
                    divider.setAttribute("title", "Drag to resize; double-click to reset");
                    bindDivider(divider, name);
                    cell.append(divider);
                }
                head.append(cell);
            });
        }

        function sortOn(name) {
            const same = state.view.sort_by === name;
            const dir = same
                ? (state.view.sort_dir === "asc" ? "desc" : "asc")
                : FIRST_DIRECTION[name] || "asc";
            setView({ sort_by: name, sort_dir: dir });
            onSortChange({ sort_by: name, sort_dir: dir });
        }

        /**
         * A column's left edge, dragged. The Name column takes up the slack,
         * so widening Date Modified narrows Name rather than pushing the
         * columns after it off the edge. Pointer capture keeps the moves
         * coming when the pointer leaves the 7px strip; nothing reparents
         * during the drag, so the capture is never lost to a redraw.
         */
        function bindDivider(divider, name) {
            divider.addEventListener("pointerdown", (event) => {
                if (event.button !== undefined && event.button !== 0) return;
                if (event.preventDefault) event.preventDefault();
                if (event.stopPropagation) event.stopPropagation();
                const columns = state.view.columns || COLUMN_DEFAULTS;
                state.resizing = { name, x: event.clientX, start: Number(columns[name]) || COLUMN_DEFAULTS[name],
                                   pointerId: event.pointerId };
                try { divider.setPointerCapture(event.pointerId); } catch (error) { /* synthetic */ }
                root.classList.add("is-resizing");
            });
            divider.addEventListener("pointermove", (event) => {
                const drag = state.resizing;
                if (!drag || drag.name !== name) return;
                const width = Math.max(COLUMN_MIN, Math.min(COLUMN_MAX,
                    Math.round(drag.start - (event.clientX - drag.x))));
                state.view.columns = { ...state.view.columns, [name]: width };
                paintColumns();
            });
            const finish = () => {
                const drag = state.resizing;
                if (!drag || drag.name !== name) return;
                state.resizing = null;
                try { divider.releasePointerCapture(drag.pointerId); } catch (error) { /* released */ }
                root.classList.remove("is-resizing");
                onColumnsChange({ ...state.view.columns });
            };
            divider.addEventListener("pointerup", finish);
            divider.addEventListener("pointercancel", finish);
            divider.addEventListener("click", (event) => {
                if (event.stopPropagation) event.stopPropagation();
            });
            divider.addEventListener("dblclick", (event) => {
                if (event.stopPropagation) event.stopPropagation();
                state.view.columns = { ...state.view.columns, [name]: COLUMN_DEFAULTS[name] };
                paintColumns();
                onColumnsChange({ ...state.view.columns });
            });
        }

        // -- what is shown ---------------------------------------------------

        function metaOf(entry) {
            return state.meta.get(entry.name) || null;
        }

        function sizeOf(entry) {
            const meta = metaOf(entry);
            const size = meta && meta.size !== undefined ? meta.size : entry.size;
            return size === undefined ? null : size;
        }

        function mtimeOf(entry) {
            const meta = metaOf(entry);
            const mtime = meta && meta.mtime !== undefined ? meta.mtime : entry.mtime;
            return mtime === undefined ? null : mtime;
        }

        //: /stat_entries never says "denied" -- only the listing's own look
        //: inside a folder finds that out -- so a chunk does not overwrite it.
        function kindOf(entry) {
            const meta = metaOf(entry);
            if (entry.kind === "denied") return "denied";
            return (meta && meta.kind) || entry.kind || (entry.is_dir ? "dir" : "file");
        }

        function shown(entry) {
            const kind = kindOf(entry);
            return kind === entry.kind || (!entry.kind && (kind === "dir" || kind === "file"))
                ? entry : { ...entry, kind };
        }

        function keyOf(entry) {
            if (entry._key === undefined) entry._key = naturalKey(entry.name);
            return entry._key;
        }

        function typeKey(entry) {
            if (entry._type === undefined) entry._type = typeLabel(entry).toLowerCase();
            return entry._type;
        }

        /** A comparator for `by`, with unknown values last whichever way. */
        function comparator(by, dir) {
            const sign = dir === "desc" ? -1 : 1;
            const byName = (a, b) => compareKeys(keyOf(a), keyOf(b));
            if (by === "mtime" || by === "size") {
                const value = by === "mtime" ? mtimeOf : (entry) => (entry.is_dir ? null : sizeOf(entry));
                return (a, b) => {
                    const va = value(a);
                    const vb = value(b);
                    const na = va === null || va === undefined;
                    const nb = vb === null || vb === undefined;
                    if (na || nb) return na === nb ? byName(a, b) : na ? 1 : -1;
                    return va === vb ? byName(a, b) : (va < vb ? -1 : 1) * sign;
                };
            }
            if (by === "kind") {
                return (a, b) => (compareKeys(typeKey(a), typeKey(b)) || byName(a, b)) * sign;
            }
            return (a, b) => byName(a, b) * sign;
        }

        /** The whole directory in display order, before the name filter. */
        function sortedEntries() {
            const view = state.view;
            const waiting = state.partial && !state.metaDone
                && (view.sort_by === "size" || view.sort_by === "mtime");
            const signature = `${state.dataVersion}|${view.sort_by}|${view.sort_dir}|${view.dirs_first}|${waiting}`;
            if (state.sorted && state.sorted.signature === signature) return state.sorted.rows;
            state.sortPending = waiting;
            const compare = waiting ? comparator("name", "asc") : comparator(view.sort_by, view.sort_dir);
            const ordered = state.entries.slice();
            if (view.dirs_first) {
                ordered.sort((a, b) => {
                    if (Boolean(a.is_dir) !== Boolean(b.is_dir)) return a.is_dir ? -1 : 1;
                    return compare(a, b);
                });
            } else {
                ordered.sort(compare);
            }
            state.sorted = { signature, rows: ordered };
            return ordered;
        }

        function derive() {
            const cursorPath = state.cursor >= 0 && state.rows[state.cursor]
                ? pathOf(state.rows[state.cursor]) : state.cursorPath;
            if (state.deep) {
                state.rows = state.deep.matches;
            } else {
                const all = sortedEntries();
                const query = state.query.trim().toLowerCase();
                state.rows = query
                    ? all.filter((entry) => String(entry.name).toLowerCase().indexOf(query) >= 0)
                    : all;
            }
            state.cursor = -1;
            if (cursorPath) {
                for (let i = 0; i < state.rows.length; i += 1) {
                    if (pathOf(state.rows[i]) === cursorPath) { state.cursor = i; break; }
                }
            }
            state.cursorPath = state.cursor >= 0 ? cursorPath : null;
            if (state.anchor >= state.rows.length) state.anchor = -1;
            state.rendered.version = -1;
        }

        // -- drawing ---------------------------------------------------------

        function isGrid() {
            return state.view.layout === "grid" && !state.deep;
        }

        function columnsInGrid() {
            return Math.max(1, Math.floor((scroller.clientWidth || 800) / TILE_W));
        }

        function visibleCount() {
            const height = scroller.clientHeight;
            return height ? Math.ceil(height / ROW_H) : FALLBACK_ROWS;
        }

        function windowRange() {
            const total = state.rows.length;
            const top = scroller.scrollTop || 0;
            if (isGrid()) {
                const cols = columnsInGrid();
                const lines = scroller.clientHeight ? Math.ceil(scroller.clientHeight / TILE_H) : 8;
                const firstLine = Math.max(0, Math.floor(top / TILE_H) - 2);
                const first = firstLine * cols;
                const last = Math.min(total, first + (lines + 4) * cols);
                return { first, last, cols };
            }
            const first = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
            const last = Math.min(total, first + visibleCount() + 2 * OVERSCAN);
            return { first, last, cols: 1 };
        }

        function render(force = false) {
            const grid = isGrid();
            const range = windowRange();
            const layout = grid ? "grid" : "list";
            const same = !force
                && state.rendered.first === range.first
                && state.rendered.last === range.last
                && state.rendered.version === state.version
                && state.rendered.layout === layout
                && state.rendered.cols === range.cols;
            if (same) {
                paintSelection();
                return;
            }
            state.rendered = { first: range.first, last: range.last, version: state.version,
                               layout, cols: range.cols };
            root.classList.toggle("is-grid", grid);
            root.classList.toggle("is-search", Boolean(state.deep));
            const total = state.rows.length;
            const lines = grid ? Math.ceil(total / range.cols) : total;
            const unit = grid ? TILE_H : ROW_H;
            spacer.style.height = `${lines * unit}px`;
            block.style.top = `${(grid ? range.first / range.cols : range.first) * unit}px`;
            if (block.style.setProperty) {
                block.style.setProperty("--plx-explorer-cols", String(range.cols));
            }
            const built = [];
            for (let index = range.first; index < range.last; index += 1) {
                built.push(grid ? buildTile(state.rows[index], index)
                                : buildRow(state.rows[index], index));
            }
            block.replaceChildren(...built);
            paintSelection();
            paintOverlay();
        }

        function rowId(index) {
            return `${domId}-row-${index}`;
        }

        function decorate(node, entry, index) {
            const verdict = usable(shown(entry), state.accept);
            node.setAttribute("role", "option");
            node.setAttribute("id", rowId(index));
            node.setAttribute("data-index", String(index));
            node.setAttribute("aria-posinset", String(index + 1));
            node.setAttribute("aria-setsize", String(state.rows.length));
            const kind = kindOf(entry);
            if (entry.is_dir) node.classList.add("is-folder");
            // A dataset only where it selects as one: in a file picker a .zarr
            // is a folder like any other, and is drawn as one.
            if (verdict.role === "dataset") node.classList.add("is-store");
            if (!verdict.ok) {
                node.classList.add("is-muted");
                node.setAttribute("aria-disabled", "true");
                if (verdict.why) node.setAttribute("title", verdict.why);
            }
            if (kind === "denied") node.classList.add("is-denied");
            if (kind === "broken") node.classList.add("is-broken");
            return verdict;
        }

        function nameCell(entry, verdict) {
            const cell = el("span", "plx-explorer-cell is-name");
            const view = shown(entry);
            cell.append(glyph(iconFor(view), "plx-explorer-icon"));
            const name = el("span", "plx-explorer-name", entry.name);
            name.setAttribute("title", entry.name);
            cell.append(name);
            if (kindOf(entry) === "link") {
                const badge = glyph("fas fa-link", "plx-explorer-badge");
                badge.setAttribute("title", "Shortcut (symbolic link)");
                cell.append(badge);
            }
            if (verdict.role === "dataset") {
                // A dataset's row answers a click by selecting it, so this is
                // the way in -- for the time somebody wants what is inside.
                const enter = button("plx-explorer-enter", "", `Open ${entry.name}`);
                enter.append(glyph("fas fa-angle-right"));
                cell.append(enter);
            }
            return cell;
        }

        function whereOf(entry) {
            const rel = String(entry.rel || "");
            const parts = rel.split(/[\\/]/).filter(Boolean);
            parts.pop();
            return parts.length ? parts.join("/") : "This folder";
        }

        function buildRow(entry, index) {
            const row = el("div", "plx-explorer-row");
            const verdict = decorate(row, entry, index);
            row.append(nameCell(entry, verdict));
            if (state.deep) {
                const where = el("span", "plx-explorer-cell is-where", whereOf(entry));
                where.setAttribute("title", entry.path || "");
                row.append(where);
                row.append(el("span", "plx-explorer-cell is-kind", typeLabel(shown(entry))));
                return row;
            }
            row.append(el("span", "plx-explorer-cell is-mtime", readableDate(mtimeOf(entry))));
            row.append(el("span", "plx-explorer-cell is-kind", typeLabel(shown(entry))));
            row.append(el("span", "plx-explorer-cell is-size",
                          entry.is_dir ? "" : readableSize(sizeOf(entry))));
            return row;
        }

        function buildTile(entry, index) {
            const tile = el("div", "plx-explorer-row plx-explorer-tile");
            const verdict = decorate(tile, entry, index);
            tile.append(glyph(iconFor(shown(entry)), "plx-explorer-icon"));
            const name = el("span", "plx-explorer-name", entry.name);
            tile.append(name);
            tile.setAttribute("title", verdict.ok ? entry.name : `${entry.name} — ${verdict.why}`);
            if (verdict.role === "dataset") {
                const enter = button("plx-explorer-enter", "", `Open ${entry.name}`);
                enter.append(glyph("fas fa-angle-right"));
                tile.append(enter);
            }
            return tile;
        }

        function renderedRows() {
            return block.children || [];
        }

        function paintSelection() {
            const rows = renderedRows();
            for (let i = 0; i < rows.length; i += 1) {
                const row = rows[i];
                const index = Number(row.getAttribute("data-index"));
                const entry = state.rows[index];
                const on = Boolean(entry) && state.selected.has(pathOf(entry));
                row.classList.toggle("is-selected", on);
                row.classList.toggle("is-cursor", index === state.cursor);
                row.setAttribute("aria-selected", on ? "true" : "false");
            }
            const cursorRendered = state.cursor >= state.rendered.first
                && state.cursor < state.rendered.last;
            scroller.setAttribute("aria-activedescendant",
                                  cursorRendered ? rowId(state.cursor) : "");
        }

        function paintOverlay() {
            const text = state.rows.length ? "" : state.emptyText;
            overlay.textContent = text || "";
            overlay.hidden = !text;
            root.classList.toggle("is-busy", state.busy);
            scroller.setAttribute("aria-busy", state.busy ? "true" : "false");
        }

        function queueRender() {
            if (state.frame) return;
            state.frame = true;
            const run = () => {
                state.frame = false;
                render();
                scrollListeners.forEach((listener) => listener());
            };
            if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(run);
            else setTimeout(run, 0);
        }

        const scrollListeners = [];
        scroller.addEventListener("scroll", queueRender);

        // -- selection -------------------------------------------------------

        function selectable(entry) {
            const verdict = usable(shown(entry), state.accept);
            return verdict.ok && (verdict.role === "file" || verdict.role === "dataset");
        }

        function setCursor(index) {
            state.cursor = index;
            state.cursorPath = index >= 0 && state.rows[index] ? pathOf(state.rows[index]) : null;
        }

        function selectOnly(entry) {
            state.selected = new Set(entry ? [pathOf(entry)] : []);
        }

        function selectRange(from, to) {
            const low = Math.min(from, to);
            const high = Math.max(from, to);
            const chosen = new Set();
            for (let i = low; i <= high; i += 1) {
                const each = state.rows[i];
                if (each && selectable(each)) chosen.add(pathOf(each));
            }
            state.selected = chosen;
        }

        function changed() {
            paintSelection();
            onSelection();
        }

        function onRowClick(index, event) {
            const entry = state.rows[index];
            if (!entry) return;
            setCursor(index);
            const verdict = usable(shown(entry), state.accept);
            if (!verdict.ok) {
                onExplain(verdict.why);
                paintSelection();
                return;
            }
            if (verdict.role === "folder") {
                // A folder is a place, and one click goes there.
                onOpen(entry);
                return;
            }
            const modifier = event && (event.metaKey || event.ctrlKey);
            if (state.accept.many) {
                if (event && event.shiftKey && state.anchor >= 0) {
                    selectRange(state.anchor, index);
                } else if (modifier) {
                    const path = pathOf(entry);
                    if (state.selected.has(path)) state.selected.delete(path);
                    else state.selected.add(path);
                    state.anchor = index;
                } else {
                    selectOnly(entry);
                    state.anchor = index;
                }
            } else {
                selectOnly(entry);
                state.anchor = index;
            }
            changed();
        }

        function onRowActivate(index) {
            const entry = state.rows[index];
            if (!entry) return;
            setCursor(index);
            const verdict = usable(shown(entry), state.accept);
            if (!verdict.ok) {
                onExplain(verdict.why);
                return;
            }
            if (verdict.role === "folder") {
                onOpen(entry);
                return;
            }
            // Double-click is "this one", on a file and on a dataset alike.
            if (!state.selected.has(pathOf(entry))) selectOnly(entry);
            changed();
            onActivate(entry);
        }

        function indexFromEvent(event) {
            const target = event && event.target;
            const row = target && target.closest ? target.closest(".plx-explorer-row") : null;
            if (!row) return -1;
            const index = Number(row.getAttribute("data-index"));
            return Number.isInteger(index) ? index : -1;
        }

        scroller.addEventListener("click", (event) => {
            const target = event && event.target;
            const enter = target && target.closest ? target.closest(".plx-explorer-enter") : null;
            const index = indexFromEvent(event);
            if (index < 0) return;
            if (enter) {
                if (event.stopPropagation) event.stopPropagation();
                setCursor(index);
                onOpen(state.rows[index]);
                return;
            }
            onRowClick(index, event);
        });
        scroller.addEventListener("dblclick", (event) => {
            const target = event && event.target;
            if (target && target.closest && target.closest(".plx-explorer-enter")) return;
            const index = indexFromEvent(event);
            if (index >= 0) onRowActivate(index);
        });
        scroller.addEventListener("contextmenu", (event) => {
            const index = indexFromEvent(event);
            if (event.preventDefault) event.preventDefault();
            const entry = index >= 0 ? state.rows[index] : null;
            if (entry) {
                setCursor(index);
                if (!state.selected.has(pathOf(entry)) && selectable(entry)) {
                    selectOnly(entry);
                    state.anchor = index;
                    changed();
                } else {
                    paintSelection();
                }
            }
            onContextMenu(entry, { x: event.clientX || 0, y: event.clientY || 0 });
        });

        // -- the cursor --------------------------------------------------------

        function ensureVisible(index) {
            if (index < 0) return;
            const grid = isGrid();
            const unit = grid ? TILE_H : ROW_H;
            const line = grid ? Math.floor(index / columnsInGrid()) : index;
            const top = line * unit;
            const height = scroller.clientHeight || visibleCount() * ROW_H;
            const scrollTop = scroller.scrollTop || 0;
            if (top < scrollTop) scroller.scrollTop = top;
            else if (top + unit > scrollTop + height) scroller.scrollTop = top + unit - height;
            render();
        }

        /**
         * Put the cursor on `index`. Single-pick fields select what the cursor
         * lands on, the way a desktop file dialog does; a folder under the
         * cursor clears the selection, since Enter on it goes in.
         */
        function moveCursorTo(index, { extend = false, keep = false } = {}) {
            const total = state.rows.length;
            if (!total) return;
            const next = Math.max(0, Math.min(total - 1, index));
            setCursor(next);
            const entry = state.rows[next];
            if (!keep) {
                if (extend && state.accept.many && state.anchor >= 0) {
                    selectRange(state.anchor, next);
                } else if (selectable(entry)) {
                    selectOnly(entry);
                    state.anchor = next;
                } else {
                    state.selected = new Set();
                    state.anchor = next;
                }
            }
            ensureVisible(next);
            changed();
        }

        function moveCursor(delta, options = {}) {
            const start = state.cursor < 0 ? (delta > 0 ? -1 : state.rows.length) : state.cursor;
            moveCursorTo(start + delta, options);
        }

        function pageSize() {
            return isGrid()
                ? Math.max(1, Math.floor((scroller.clientHeight || TILE_H * 4) / TILE_H)) * columnsInGrid()
                : Math.max(1, Math.floor((scroller.clientHeight || ROW_H * 20) / ROW_H) - 1);
        }

        function toggleCursor() {
            const entry = state.rows[state.cursor];
            if (!entry || !selectable(entry)) return;
            const path = pathOf(entry);
            if (state.accept.many) {
                if (state.selected.has(path)) state.selected.delete(path);
                else state.selected.add(path);
            } else {
                selectOnly(entry);
            }
            state.anchor = state.cursor;
            changed();
        }

        function selectAll() {
            if (!state.accept.many) return;
            state.selected = new Set(state.rows.filter(selectable).map(pathOf));
            changed();
        }

        function clearSelection() {
            if (!state.selected.size) return;
            state.selected = new Set();
            changed();
        }

        /** Enter on the cursor row: what a double-click would do. */
        function activateCursor() {
            if (state.cursor < 0 || !state.rows[state.cursor]) return false;
            onRowActivate(state.cursor);
            return true;
        }

        // -- what the controller hands in --------------------------------------

        function setListing(entries, { partial = false, query = null } = {}) {
            state.entries = Array.isArray(entries) ? entries : [];
            if (query !== null) state.query = String(query);
            state.partial = Boolean(partial);
            state.meta = new Map();
            state.metaDone = !state.partial;
            state.deep = null;
            state.version += 1;
            state.dataVersion += 1;
            state.sorted = null;
            state.selected = new Set();
            state.anchor = -1;
            state.cursor = -1;
            state.cursorPath = null;
            scroller.scrollTop = 0;
            derive();
            renderHead();
            render(true);
        }

        /** Sizes and dates for rows the listing sent without them. */
        function mergeMetadata(updates) {
            let any = false;
            Object.keys(updates || {}).forEach((name) => {
                state.meta.set(name, updates[name]);
                any = true;
            });
            if (!any) return;
            // No re-sort: rows do not move under the pointer as each chunk
            // lands. The visible ones are redrawn with what they now know.
            state.version += 1;
            render(true);
        }

        function metadataComplete() {
            if (state.metaDone) return;
            state.metaDone = true;
            state.version += 1;
            state.dataVersion += 1;
            derive();
            renderHead();
            if (state.cursor >= 0) ensureVisible(state.cursor);
            render(true);
        }

        function setQuery(query) {
            const next = String(query || "");
            if (next === state.query) return;
            state.query = next;
            state.version += 1;
            derive();
            scroller.scrollTop = 0;
            render(true);
        }

        /** Deep-search answers in place of the directory; null goes back. */
        function setSearchResults(matches) {
            state.deep = Array.isArray(matches) ? { matches } : null;
            state.version += 1;
            state.selected = new Set();
            state.anchor = -1;
            state.cursor = -1;
            state.cursorPath = null;
            scroller.scrollTop = 0;
            derive();
            renderHead();
            render(true);
        }

        function setView(next) {
            const merged = { ...state.view, ...(next || {}) };
            merged.columns = { ...COLUMN_DEFAULTS, ...(state.view.columns || {}), ...((next && next.columns) || {}) };
            state.view = merged;
            paintColumns();
            derive();
            renderHead();
            render(true);
        }

        function setAccept(next) {
            state.accept = next;
            scroller.setAttribute("aria-multiselectable", next && next.many ? "true" : "false");
            render(true);
        }

        function setStatus({ busy, empty } = {}) {
            if (busy !== undefined) state.busy = Boolean(busy);
            if (empty !== undefined) state.emptyText = empty || "";
            paintOverlay();
        }

        /** The selected rows, in the order they are drawn in. */
        function selection() {
            if (!state.selected.size) return [];
            const source = state.deep ? state.deep.matches : sortedEntries();
            return source.filter((entry) => state.selected.has(pathOf(entry)));
        }

        function visibleRange() {
            const range = windowRange();
            return { first: range.first, last: range.last };
        }

        setAccept(accept);
        paintColumns();
        renderHead();

        return {
            element: root,
            list: scroller,
            setAccept,
            setListing,
            mergeMetadata,
            metadataComplete,
            metadataDone: () => state.metaDone,
            setQuery,
            query: () => state.query,
            setSearchResults,
            isSearching: () => Boolean(state.deep),
            setView,
            view: () => ({ ...state.view, columns: { ...state.view.columns } }),
            setStatus,
            selection,
            cursorEntry: () => state.rows[state.cursor] || null,
            cursorIndex: () => state.cursor,
            rows: () => state.rows,
            rowsCount: () => state.rows.length,
            entriesCount: () => state.entries.length,
            entries: () => state.entries,
            meta: (name) => state.meta.get(name) || null,
            sizeOf,
            mtimeOf,
            visibleRange,
            onScroll(listener) { scrollListeners.push(listener); },
            moveCursor,
            moveCursorTo,
            pageSize,
            gridColumns: () => (isGrid() ? columnsInGrid() : 1),
            isGrid,
            toggleCursor,
            selectAll,
            clearSelection,
            activateCursor,
            ensureVisible,
            focus() { if (scroller.focus) scroller.focus(); },
            relayout() { render(true); },
        };
    }

    return { create, naturalKey, ROW_H, COLUMN_DEFAULTS, DEFAULT_VIEW };
})();
