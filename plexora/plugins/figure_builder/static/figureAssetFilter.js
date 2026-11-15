/**
 * FigureAssetFilter - the Assets tab's filter: narrow captures, bookmarks and
 * uploads by project (and the dataset holding it), file, channel, location
 * and date.
 *
 * A button beside the search opens a menu: one search at the top, which
 * narrows every list in it at once, then one section per criterion. Projects
 * are listed under the dataset that holds them, and a dataset is ticked like
 * a project -- ticking one keeps everything from any of its projects. Within
 * a criterion the ticked values are alternatives (CD8 or CD4); across
 * criteria they all have to hold (CD8, in this dataset, this week). A
 * criterion is offered only when the tab's items differ on it. What is on is
 * shown as chips under the Assets search, each with its own ×.
 *
 * The options come from whatever the tab is showing (`setItems`), so the menu
 * never offers a value that would empty the list.
 */
class FigureAssetFilter {

    /** The criteria, in the menu's order. "project" is the dataset/project
     *  tree; "date" is one choice; the rest any number. */
    static get CRITERIA() {
        return [
            { key: "project", title: "Project" },
            { key: "file", title: "File" },
            { key: "channel", title: "Channel" },
            { key: "location", title: "Location" },
            { key: "date", title: "Date" },
        ];
    }

    /** What each kind of chip is called. */
    static get TITLES() {
        return { dataset: "Dataset", project: "Project", file: "File",
                 channel: "Channel", location: "Location", date: "Date" };
    }

    static get DATES() {
        return [
            { key: "today", label: "Today", days: 1 },
            { key: "week", label: "Past 7 days", days: 7 },
            { key: "month", label: "Past 30 days", days: 30 },
            { key: "older", label: "Older than 30 days", days: -30 },
        ];
    }

    /**
     * What a capture, bookmark or figure image can be filtered by. Both
     * shapes carry a `source` and a `scene`, and a date in `created_at`.
     *
     * In Plexora a project is one image (one config.json key), so "project"
     * is the image a capture was taken from, named as the library names it.
     * Its dataset and location are not stored on a capture: they are looked
     * up in `projects` (core's `GET /projects`, by key) when the filter is
     * drawn, which is also right for a capture taken before a project was
     * put in a dataset. An uploaded file has no project; it is its file.
     */
    static facetsOf(item, projects) {
        const source = (item && item.source) || {};
        const scene = (item && item.scene) || {};
        const uploaded = source.kind === "imported_asset";
        const key = uploaded ? "" : (source.datasource || (item && item.datasource) || "");
        const card = key && projects ? projects.get(key) : null;
        return {
            dataset: (card && card.dataset && card.dataset.name) || "",
            project: key ? ((card && (card.displayName || card.title)) || source.display_name || key) : "",
            file: uploaded ? (source.display_name || "") : "",
            channel: (scene.channels || [])
                .map((channel) => channel.fullname_at_capture || channel.key).filter(Boolean),
            location: (card && card.location && card.location.label) || "",
            date: (item && item.created_at) || "",
        };
    }

    /**
     * @param {object} options button, chips (where the chips go), root (where
     *        the menu is put, for the workspace's colours), onChange().
     */
    constructor(options) {
        this.button = options.button;
        this.chipsHost = options.chips;
        this.root = options.root || document.body;
        this.onChange = options.onChange || (() => {});
        this.picked = { dataset: new Set(), project: new Set(), file: new Set(),
                        channel: new Set(), location: new Set() };
        this.date = "";
        this.items = [];
        this.itemsKey = "";
        this.query = "";
        this.menu = null;
        //: The one section open (null: all shut, as the menu opens).
        this.openSection = null;

        this.button?.addEventListener("click", (event) => {
            event.stopPropagation();
            if (this.menu) this.close();
            else this.open();
        });
        this.chipsHost?.addEventListener("click", (event) => {
            const chip = event.target.closest("[data-chip]");
            if (!chip) return;
            if (chip.dataset.chip === "clear") this.clear();
            else this.unpick(chip.dataset.chip, chip.dataset.value);
        });
        this.render();
    }

    /** Is any criterion on? */
    get active() {
        return Boolean(this.date) || Object.values(this.picked).some((set) => set.size);
    }

    /** The facets of everything the tab could show, for the options. Called
     *  on every redraw of the tab, so the menu is redrawn only when they
     *  actually changed -- a redraw would lose a long list's scroll. */
    setItems(facetsList) {
        const items = facetsList || [];
        const key = JSON.stringify(items);
        if (key === this.itemsKey) return;
        this.items = items;
        this.itemsKey = key;
        if (this.menu) this.drawBodies();
    }

    /** Does an item with these facets pass every criterion that is on? */
    matches(facets) {
        // Datasets and projects are one criterion: in a ticked dataset, or a
        // ticked project.
        const datasets = this.picked.dataset;
        const projects = this.picked.project;
        if ((datasets.size || projects.size)
                && !(datasets.has(facets.dataset) || projects.has(facets.project))) {
            return false;
        }
        for (const key of ["file", "channel", "location"]) {
            const set = this.picked[key];
            if (!set.size) continue;
            const values = Array.isArray(facets[key]) ? facets[key] : [facets[key]];
            if (!values.some((value) => set.has(value))) return false;
        }
        if (this.date) {
            const bucket = FigureAssetFilter.DATES.find((entry) => entry.key === this.date);
            const when = Date.parse(facets.date || "");
            if (!bucket || !Number.isFinite(when)) return false;
            const days = (Date.now() - when) / 86400000;
            if (bucket.days > 0 && days > bucket.days) return false;
            if (bucket.days < 0 && days <= -bucket.days) return false;
        }
        return true;
    }

    clear() {
        Object.values(this.picked).forEach((set) => set.clear());
        this.date = "";
        this.changed();
    }

    unpick(key, value) {
        if (key === "date") this.date = "";
        else this.picked[key]?.delete(value);
        this.changed();
    }

    changed() {
        this.render();
        if (this.menu) this.drawBodies();
        this.onChange();
    }

    /** Every value a criterion takes across the items, with how many items
     *  have it, most first. */
    options(key) {
        const counts = new Map();
        for (const facets of this.items) {
            const values = Array.isArray(facets[key]) ? facets[key] : [facets[key]];
            for (const value of new Set(values.filter(Boolean))) {
                counts.set(value, (counts.get(value) || 0) + 1);
            }
        }
        // Keep a value that is ticked even when nothing shown has it now.
        for (const value of this.picked[key] || []) {
            if (!counts.has(value)) counts.set(value, 0);
        }
        return Array.from(counts.entries())
            .sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])));
    }

    /**
     * The projects, under the datasets that hold them: `[{dataset, count,
     * projects: [[name, count]]}]`, datasets by name with the projects in no
     * dataset last (`dataset` ""), each list narrowed by the menu's search --
     * a dataset whose name matches keeps all its projects.
     */
    projectTree() {
        const groups = new Map();
        for (const facets of this.items) {
            if (!facets.project) continue;
            const name = facets.dataset || "";
            if (!groups.has(name)) groups.set(name, { dataset: name, count: 0, projects: new Map() });
            const group = groups.get(name);
            group.count += 1;
            group.projects.set(facets.project, (group.projects.get(facets.project) || 0) + 1);
        }
        const query = this.query;
        return Array.from(groups.values())
            .sort((a, b) => (!a.dataset) - (!b.dataset) || a.dataset.localeCompare(b.dataset))
            .map((group) => {
                const whole = group.dataset && FigureSchema.matchesSearch(group.dataset, query);
                const projects = Array.from(group.projects.entries())
                    .filter(([name]) => whole || FigureSchema.matchesSearch(name, query))
                    .sort((a, b) => a[0].localeCompare(b[0]));
                return { ...group, projects };
            })
            .filter((group) => group.projects.length);
    }

    // -- the button and the chips ------------------------------------------

    render() {
        const count = Object.values(this.picked).reduce((sum, set) => sum + set.size, 0)
            + (this.date ? 1 : 0);
        if (this.button) {
            this.button.classList.toggle("is-on", count > 0);
            this.button.setAttribute("aria-expanded", String(Boolean(this.menu)));
            const badge = this.button.querySelector("[data-role=badge]");
            if (badge) {
                badge.textContent = count ? String(count) : "";
                badge.hidden = !count;
            }
        }
        if (!this.chipsHost) return;
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const titles = FigureAssetFilter.TITLES;
        const chips = [];
        for (const key of ["dataset", "project", "file", "channel", "location"]) {
            for (const value of this.picked[key]) chips.push([key, value, value]);
        }
        const bucket = FigureAssetFilter.DATES.find((entry) => entry.key === this.date);
        if (bucket) chips.push(["date", this.date, bucket.label]);
        this.chipsHost.hidden = !chips.length;
        this.chipsHost.innerHTML = chips.map(([key, value, label]) =>
            `<button type="button" class="fb-filter-chip" data-chip="${escape(key)}"
                     data-value="${escape(value)}" title="Remove this filter"
                     aria-label="Remove ${escape(titles[key])}: ${escape(label)}">
                <span class="fb-filter-chip-key">${escape(titles[key])}</span>
                <span class="fb-filter-chip-value">${escape(label)}</span>
                ${FigureIcons.svg("x", 11)}
            </button>`).join("")
            + (chips.length > 1
                ? '<button type="button" class="fb-filter-clear" data-chip="clear">Clear all</button>' : "");
    }

    // -- the menu -----------------------------------------------------------

    open() {
        const menu = document.createElement("div");
        menu.className = "fb-menu fb-filter-menu";
        menu.setAttribute("role", "dialog");
        menu.setAttribute("aria-label", "Filter assets");
        this.root.appendChild(menu);
        this.menu = menu;
        this.drawMenu();
        this.place();

        menu.addEventListener("click", (event) => this.menuClicked(event));
        menu.addEventListener("input", (event) => {
            if (!event.target.matches("[data-role=find]")) return;
            this.query = event.target.value || "";
            this.drawBodies();
        });
        this.outside = (event) => {
            if (!this.menu || this.menu.contains(event.target) || this.button?.contains(event.target)) return;
            this.close();
        };
        this.escape = (event) => {
            if (event.key !== "Escape") return;
            event.stopPropagation();
            // First out of the search, then out of the menu.
            if (event.target.matches?.("[data-role=find]") && this.query) {
                event.target.value = "";
                this.query = "";
                this.drawBodies();
                return;
            }
            this.close();
            this.button?.focus();
        };
        document.addEventListener("pointerdown", this.outside, true);
        menu.addEventListener("keydown", this.escape);
        this.render();
        menu.querySelector("[data-role=find]")?.focus();
    }

    close() {
        document.removeEventListener("pointerdown", this.outside, true);
        this.menu?.remove();
        this.menu = null;
        this.query = "";
        this.openSection = null;
        this.render();
    }

    /** Under the button, kept inside the window with an 8px margin: above
     *  it when there is more room there and the menu does not fit below,
     *  and never taller than the space on the side it opens to (it scrolls
     *  inside instead). Opened above, it is pinned by its bottom edge, so a
     *  section that opens grows it upward, away from the button. */
    place() {
        if (!this.menu || !this.button) return;
        const margin = 8;
        const gap = 4;
        const anchor = this.button.getBoundingClientRect();
        const width = Math.min(272, window.innerWidth - 2 * margin);
        const left = Math.max(margin, Math.min(anchor.left, window.innerWidth - width - margin));
        const below = window.innerHeight - anchor.bottom - gap - margin;
        const above = anchor.top - gap - margin;
        const menu = this.menu;
        menu.style.left = `${left}px`;
        menu.style.width = `${width}px`;
        menu.style.maxHeight = "";
        // offsetHeight, not the bounding box: the pop-in animation scales it.
        const wanted = menu.offsetHeight;
        const flip = wanted > below && above > below;
        const room = Math.max(120, Math.floor(flip ? above : below));
        if (flip) {
            menu.style.top = "auto";
            menu.style.bottom = `${window.innerHeight - anchor.top + gap}px`;
            menu.style.transformOrigin = "bottom left";
        } else {
            menu.style.top = `${anchor.bottom + gap}px`;
            menu.style.bottom = "auto";
            menu.style.transformOrigin = "";
        }
        menu.style.maxHeight = `min(70vh, 480px, ${room}px)`;
    }

    /** The frame: the head with its search, and an empty shell per
     *  criterion that `drawBodies` fills. */
    drawMenu() {
        if (!this.menu) return;
        // Folded, one open at a time: a head per criterion that opens its
        // list and shuts the others, so the menu stays short and no list
        // takes the scroll from the rest.
        const sections = FigureAssetFilter.CRITERIA.map(({ key, title }) => `
            <section class="fb-filter-section" data-section="${key}">
                <div class="fb-filter-title">
                    <button type="button" class="fb-filter-toggle" data-toggle="${key}"
                            aria-expanded="false" aria-controls="fb_filter_${key}">
                        ${FigureIcons.svg("chevR", 12, "fb-filter-chev")}
                        <span>${title}</span>
                        <span class="fb-filter-summary" data-summary="${key}"></span>
                    </button>
                    <button type="button" class="fb-filter-reset" data-reset="${key}" hidden>Clear</button>
                </div>
                <div class="fb-filter-options" id="fb_filter_${key}" data-options="${key}" role="group"
                     aria-label="${title}"></div>
            </section>`).join("");
        this.menu.innerHTML = `
            <div class="fb-filter-head">
                <div class="fb-filter-head-row">
                    <strong>Filter</strong>
                    <button type="button" class="fb-filter-reset" data-reset="all">Clear all</button>
                </div>
                <label class="fb-filter-find">${FigureIcons.svg("search", 12)}
                    <input type="search" data-role="find" autocomplete="off" spellcheck="false"
                           placeholder="Search projects, datasets, channels…"
                           aria-label="Search the filter's options"
                           value="${FigureSchema.escapeHtml(this.query)}"></label>
            </div>
            <div class="fb-filter-sections">${sections}</div>
            <p class="fb-filter-none" data-role="none" hidden>Nothing matches.</p>`;
        this.drawBodies();
    }

    /**
     * Fill every section for the items and the search. A section shows when
     * it has something to choose between (or something ticked); while
     * searching, only sections with a match show, and Date is left out
     * unless its own words match.
     */
    drawBodies() {
        if (!this.menu) return;
        const searching = Boolean(this.query.trim());
        let shownAny = false;
        for (const { key } of FigureAssetFilter.CRITERIA) {
            const section = this.menu.querySelector(`[data-section="${key}"]`);
            const body = this.menu.querySelector(`[data-options="${key}"]`);
            if (!section || !body) continue;
            let html = "";
            let offered = false;
            let ticked = false;
            if (key === "date") {
                const dates = FigureAssetFilter.DATES
                    .filter((entry) => !searching || FigureSchema.matchesSearch(`date ${entry.label}`, this.query));
                offered = dates.length > 0;
                ticked = Boolean(this.date);
                html = `<div class="fb-filter-dates">${dates.map((entry) => `
                    <button type="button" class="fb-filter-date${this.date === entry.key ? " is-on" : ""}"
                            data-date="${entry.key}" aria-pressed="${this.date === entry.key}">
                        ${FigureSchema.escapeHtml(entry.label)}</button>`).join("")}</div>`;
            } else if (key === "project") {
                const tree = this.projectTree();
                const all = this.options("project");
                ticked = this.picked.project.size > 0 || this.picked.dataset.size > 0;
                offered = tree.length > 0 && (all.length > 1 || tree.some((g) => g.dataset) || ticked);
                html = this.treeHtml(tree);
            } else {
                const all = this.options(key);
                ticked = this.picked[key].size > 0;
                const rows = all.filter(([value]) => FigureSchema.matchesSearch(value, this.query));
                offered = rows.length > 0 && (all.length > 1 || ticked);
                html = rows.map(([value, count]) => this.rowHtml(key, value, count)).join("");
            }
            section.hidden = !offered;
            const reset = section.querySelector(`[data-reset="${key}"]`);
            if (reset) reset.hidden = !ticked;
            // Open: the one chosen, or, while searching, every section with a
            // match -- a search that found something inside a shut section
            // would look like a search that found nothing.
            const open = offered && (searching || this.openSection === key);
            section.classList.toggle("is-open", open);
            body.hidden = !open;
            const toggle = section.querySelector(`[data-toggle="${key}"]`);
            if (toggle) toggle.setAttribute("aria-expanded", String(open));
            const summary = section.querySelector(`[data-summary="${key}"]`);
            if (summary) summary.textContent = this.summary(key);
            if (offered) {
                // Kept where it was scrolled to: a tick redraws the list.
                const scroll = body.scrollTop;
                body.innerHTML = html;
                body.scrollTop = scroll;
                shownAny = true;
            }
        }
        const none = this.menu.querySelector("[data-role=none]");
        if (none) {
            none.hidden = shownAny;
            none.textContent = searching ? "Nothing matches." : "Nothing to filter by yet.";
        }
        const all = this.menu.querySelector('[data-reset="all"]');
        if (all) all.disabled = !this.active;
    }

    /** What a shut section says it has on: the one value, or how many. */
    summary(key) {
        if (key === "date") {
            return FigureAssetFilter.DATES.find((entry) => entry.key === this.date)?.label || "";
        }
        const values = key === "project"
            ? [...this.picked.dataset, ...this.picked.project] : [...this.picked[key]];
        if (!values.length) return "";
        return values.length === 1 ? values[0] : `${values.length} selected`;
    }

    /** Datasets as tickable rows, their projects indented under them; the
     *  projects in no dataset after, unheaded when no dataset exists. */
    treeHtml(tree) {
        const anyDataset = tree.some((group) => group.dataset);
        return tree.map((group) => {
            const head = group.dataset
                ? this.rowHtml("dataset", group.dataset, group.count, "fb-filter-dataset")
                : anyDataset ? '<div class="fb-filter-subhead">Not in a dataset</div>' : "";
            const rows = group.projects.map(([name, count]) =>
                this.rowHtml("project", name, count, group.dataset ? "fb-filter-child" : "")).join("");
            return head + rows;
        }).join("");
    }

    rowHtml(key, value, count, extra = "") {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const on = this.picked[key].has(value);
        return `<button type="button" class="fb-filter-option${on ? " is-on" : ""}${extra ? " " + extra : ""}"
                        data-pick="${escape(key)}" data-value="${escape(value)}"
                        role="checkbox" aria-checked="${on}" title="${escape(value)}">
            <span class="fb-filter-box">${on ? FigureIcons.svg("check", 10) : ""}</span>
            ${key === "dataset" ? `<span class="fb-filter-kind">${FigureIcons.svg("layers", 12)}</span>` : ""}
            <span class="fb-filter-label">${escape(value)}</span>
            <span class="fb-filter-count">${count}</span>
        </button>`;
    }

    menuClicked(event) {
        const toggle = event.target.closest("[data-toggle]");
        if (toggle) {
            const key = toggle.dataset.toggle;
            // While searching every matching section is open; a click on a
            // head then leaves the search and opens that one alone.
            if (this.query.trim()) {
                this.query = "";
                const find = this.menu?.querySelector("[data-role=find]");
                if (find) find.value = "";
                this.openSection = key;
            } else {
                this.openSection = this.openSection === key ? null : key;
            }
            this.drawBodies();
            return;
        }
        const pick = event.target.closest("[data-pick]");
        if (pick) {
            const set = this.picked[pick.dataset.pick];
            const value = pick.dataset.value;
            if (set.has(value)) set.delete(value);
            else set.add(value);
            this.changed();
            return;
        }
        const date = event.target.closest("[data-date]");
        if (date) {
            this.date = this.date === date.dataset.date ? "" : date.dataset.date;
            this.changed();
            return;
        }
        const reset = event.target.closest("[data-reset]");
        if (reset) {
            const key = reset.dataset.reset;
            if (key === "all") {
                this.clear();
                return;
            }
            if (key === "date") this.date = "";
            else if (key === "project") {
                this.picked.project.clear();
                this.picked.dataset.clear();
            } else this.picked[key]?.clear();
            this.changed();
        }
    }
}
