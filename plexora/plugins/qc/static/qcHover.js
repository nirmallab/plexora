/**
 * qcHover.js - what a QC region or a flagged cell says when the pointer rests
 * on it.
 *
 * A small dark card beside the pointer, in the manner of Xenium Explorer's
 * cell card: the finding's category, what it is, whether it excludes or only
 * warns, then the one line that explains it -- the agent's own words when it
 * left some, else a sentence built from the score and the bar it crossed --
 * and a few label/value rows. Several reasons on one cell read as the primary
 * one first, the rest listed under it. The whole provenance stays in the
 * panel's Details and in the exports; the card is the short answer.
 *
 * Two parts, kept apart so either can be checked alone:
 *
 * - `QcHoverCard`: the card itself (portaled, never takes the pointer) and
 *   the pure builders that turn a region or a cell record into the card's
 *   model -- `{title, chip, status, lead, note, rows, sections, footer}`.
 * - `QcHoverProbe`: the pointer. One hit test a frame against QC's region
 *   overlay (`QcRegionOverlay.hitTest`); while QC's cell layer is on, a
 *   debounced ask of the server for the cell under the pointer
 *   (`QcApi.cellAt`, which reads the mask). The cell card wins when there is
 *   one -- it names the region the cell is in. A press, a drag, a wheel, the
 *   pointer leaving, a stroke being drawn or the ROI tool on screen all clear
 *   it. A click on a region opens it in the panel (`onSelect`).
 *
 * Loaded before qcSidebarController.js, so nothing here names the controller:
 * it hands its words and colours in as `helpers`.
 */
class QcHoverCard {

    static get EDGE_PAD() { return 8; }
    static get GAP() { return 14; }
    /** How many of a cell's other reasons are listed before "+n more". */
    static get MAX_ITEMS() { return 4; }

    static get SCORE_KIND_WORDS() {
        return {
            blur_score: "Blur score",
            mismatch_share: "Mismatch share",
            flagged_cell_density: "Flagged-cell density",
            detector_score: "Detector score",
        };
    }

    static get SOURCE_WORDS() {
        return {
            auto: "automatic",
            agent_refined: "agent-refined",
            user: "set by hand",
            user_relative: "moved by hand",
        };
    }

    static get VERDICT_WORDS() {
        return {
            artifact: "an artifact",
            not_artifact: "real tissue",
            need_more_evidence: "unclear without a closer look",
            cannot_tell: "unclear",
            normal: "normal",
            mixed: "mixed",
            accept: "right",
            too_lenient: "too lenient",
            too_aggressive: "too aggressive",
        };
    }

    /** The units a cell module's cutoff is in, said after the number. */
    static get SPACE_WORDS() {
        return { log: "log", log1p: "log", log10_ratio: "log ratio", robust_z: "z" };
    }

    static get CHECK_WORDS() {
        return { blur: "Blur QC", registration: "Registration QC", segmentation: "Segmentation QC" };
    }

    constructor() {
        this.card = null;
        this.model = null;
    }

    get visible() {
        return Boolean(this.card && !this.card.hidden);
    }

    ensure() {
        if (this.card) return this.card;
        const card = document.createElement("div");
        card.className = "qc-hover-card";
        card.setAttribute("role", "tooltip");
        card.hidden = true;
        // Portaled, like Cell Explorer's card: the anchor is in client pixels
        // and no ancestor's overflow may clip a card near the image's edge.
        if (window.PopoverPortal) window.PopoverPortal.attach(card);
        else document.body.appendChild(card);
        this.card = card;
        return card;
    }

    /** Show `model` beside the client point `anchor` ({x, y}), inside
     *  `bounds` (the viewer canvas's client rect). */
    show(model, anchor, bounds) {
        const card = this.ensure();
        if (model !== this.model) {
            this.render(model);
            this.model = model;
        }
        card.hidden = false;
        this.place(anchor, bounds);
    }

    hide() {
        if (this.card) this.card.hidden = true;
        this.model = null;
    }

    destroy() {
        if (!this.card) return;
        if (window.PopoverPortal) window.PopoverPortal.detach(this.card);
        this.card.remove?.();
        this.card = null;
        this.model = null;
    }

    /**
     * Below and to the right of the pointer, flipped to the left or above
     * where the image ends, and never outside it: the card stays beside the
     * thing described and off the patch of tissue under the pointer.
     */
    place(anchor, bounds) {
        const card = this.card;
        if (!card || !anchor) return;
        const pad = QcHoverCard.EDGE_PAD;
        const gap = QcHoverCard.GAP;
        const area = bounds || { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
        const box = card.getBoundingClientRect();
        const width = box.width || 0;
        const height = box.height || 0;
        let left = anchor.x + gap;
        if (left + width > area.right - pad) left = anchor.x - gap - width;
        left = Math.max(area.left + pad, Math.min(left, area.right - pad - width));
        let top = anchor.y + gap;
        if (top + height > area.bottom - pad) top = anchor.y - gap - height;
        top = Math.max(area.top + pad, Math.min(top, area.bottom - pad - height));
        card.style.left = `${Math.round(left)}px`;
        card.style.top = `${Math.round(top)}px`;
    }

    render(model) {
        const card = this.ensure();
        card.replaceChildren?.();
        if (!card.replaceChildren) while (card.firstChild) card.removeChild(card.firstChild);
        const el = (tag, className, text) => {
            const node = document.createElement(tag);
            if (className) node.className = className;
            if (text !== undefined && text !== null) node.textContent = String(text);
            return node;
        };
        const head = el("div", "qc-hover-head");
        if (model.chip) {
            const chip = el("span", "qc-hover-chip");
            const dot = el("span", "qc-hover-dot");
            dot.style.setProperty?.("--qc-row-color", model.chip.color || "#9ca3af");
            chip.append(dot, el("span", "qc-hover-chip-words", model.chip.words));
            head.append(chip);
        }
        if (model.status) {
            const status = el("span", "qc-hover-status", model.status.words);
            status.dataset.tone = model.status.tone || "muted";
            head.append(status);
        }
        card.append(head);
        card.append(el("div", "qc-hover-title", model.title));
        if (model.lead) card.append(el("div", "qc-hover-lead", model.lead));
        if (model.note) card.append(el("div", "qc-hover-note", model.note));
        if ((model.rows || []).length) {
            const list = el("dl", "qc-hover-rows");
            for (const [label, value] of model.rows) {
                list.append(el("dt", null, label), el("dd", null, value));
            }
            card.append(list);
        }
        for (const section of model.sections || []) {
            if (!(section.items || []).length) continue;
            const block = el("div", "qc-hover-section");
            block.append(el("div", "qc-hover-heading", section.heading));
            const list = el("ul", "qc-hover-list");
            for (const item of section.items) {
                const line = el("li", item.more ? "qc-hover-more" : null);
                if (item.color) {
                    const dot = el("span", "qc-hover-dot");
                    dot.style.setProperty?.("--qc-row-color", item.color);
                    line.append(dot);
                }
                line.append(el("span", "qc-hover-item", item.text));
                if (item.detail) line.append(el("span", "qc-hover-detail", item.detail));
                list.append(line);
            }
            block.append(list);
            card.append(block);
        }
        if (model.footer) card.append(el("div", "qc-hover-foot", model.footer));
    }

    // -- the words ------------------------------------------------------------------

    /** A number as the card shows it: three significant figures at most. */
    static number(value) {
        if (typeof value !== "number" || !Number.isFinite(value)) return "";
        const magnitude = Math.abs(value);
        if (magnitude >= 1000) return Math.round(value).toLocaleString("en-US");
        if (magnitude >= 100) return value.toFixed(0);
        if (magnitude >= 10) return value.toFixed(1).replace(/\.0$/, "");
        if (magnitude === 0) return "0";
        return Number(value.toPrecision(3)).toString();
    }

    static words(token) {
        return String(token || "").replace(/_/g, " ");
    }

    static lowerFirst(text) {
        const s = String(text || "");
        return s ? s[0].toLowerCase() + s.slice(1) : s;
    }

    static percent(fraction) {
        return typeof fraction === "number" && Number.isFinite(fraction)
            ? `${Math.round(fraction * 100)}%` : "";
    }

    static steps(offset) {
        if (!offset) return "";
        return `${offset > 0 ? "+" : ""}${offset} step${Math.abs(offset) === 1 ? "" : "s"}`;
    }

    /** "automatic", or "agent-refined, +1 step". */
    static barSource(source, offset) {
        const words = QcHoverCard.SOURCE_WORDS[source] || QcHoverCard.words(source || "auto");
        const steps = QcHoverCard.steps(offset);
        return steps ? `${words}, ${steps}` : words;
    }

    /** "artifact · sure · severe" from an agent's judgment. */
    static agentWords(ai) {
        if (!ai) return "";
        return [ai.verdict, ai.confidence, ai.severity].filter(Boolean)
            .map((v) => QcHoverCard.words(v)).join(" · ");
    }

    // -- a region -------------------------------------------------------------------

    /**
     * A region's card. Drawn by hand: what it is and what it does. Found by an
     * image check or a detector: also why -- the agent's notes, or the score
     * over the bar -- with the channels, the agent's judgment and the cells
     * it takes out.
     */
    static regionModel(region, helpers) {
        if (!region) return null;
        const h = helpers || {};
        const capital = h.capital || ((s) => String(s || ""));
        const tool = region.tool || {};
        const ai = region.ai || {};
        const origin = (h.originOf && h.originOf(region)) || { manual: tool.name === "user", words: "" };
        const manual = Boolean(origin.manual || tool.origin === "user");
        const check = tool.origin === "check";
        const n = QcHoverCard.number;
        const rows = [];
        const add = (label, value) => {
            if (value !== undefined && value !== null && value !== "") rows.push([label, String(value)]);
        };
        const subtype = capital(region.words || (h.classWords ? h.classWords(region.class) : region.class));
        // QC names its own ROIs "QC exclude: tissue fold · DNA_2"; a name the
        // user gave is kept, and a found region otherwise goes by its subtype.
        const named = region.name && !/^QC [^:]*:/.test(region.name);
        const title = manual || named
            ? (h.regionName ? h.regionName(region) : (region.name || subtype)) : subtype;
        const defaultClass = h.defaultClass ? h.defaultClass(region.category) : null;
        if (!region.custom && region.class && region.class !== defaultClass && title !== subtype) {
            add("Subtype", subtype);
        }
        let lead = "";
        let note = "";
        if (manual) {
            const traced = h.tracedWords ? h.tracedWords(region) : "";
            lead = ["Drawn by hand", traced].filter(Boolean).join(" · ");
            const drawn = (region.view_channels || []).map((v) => v.name).filter(Boolean);
            if (drawn.length) add("Drawn on", drawn.join(", "));
        } else {
            const kind = QcHoverCard.SCORE_KIND_WORDS[region.score_kind]
                || capital(QcHoverCard.words(region.score_kind || "score"));
            const scored = typeof region.score === "number";
            const barred = typeof region.threshold === "number";
            const judged = ai.verdict ? QcHoverCard.VERDICT_WORDS[ai.verdict] || QcHoverCard.words(ai.verdict) : "";
            let composed;
            if (check && scored && barred) {
                composed = `${kind} ${n(region.score)} over the bar ${n(region.threshold)}`
                    + (judged ? `; the agent judged it ${judged}` : "");
            } else {
                const finder = check ? (QcHoverCard.CHECK_WORDS[tool.name] || capital(QcHoverCard.words(tool.name)))
                    : `the ${QcHoverCard.words(tool.name || "detector")} detector`;
                composed = `Found by ${finder}`
                    + (judged ? `; the agent judged it ${judged}` : "");
            }
            if (ai.notes) {
                lead = ai.notes;
                note = composed;
            } else {
                lead = composed;
            }
            if (scored && !(lead === composed && check && barred)) {
                add("Score", `${n(region.score)}${barred ? ` · bar ${n(region.threshold)}` : ""}`);
            }
            if (barred && (region.threshold_source && region.threshold_source !== "auto" || region.offset_steps)) {
                add("Bar", QcHoverCard.barSource(region.threshold_source, region.offset_steps));
            }
            const channels = region.evidence_channels && region.evidence_channels.length
                ? region.evidence_channels : region.channels;
            add("Channels", (channels || []).join(", ") || "all");
            if ((region.cycles || []).length) add("Cycle", region.cycles.join(", "));
            const agent = QcHoverCard.agentWords(ai);
            if (agent) add("Agent", agent);
            else if (region.severity && typeof region.severity === "string") add("Severity", region.severity);
        }
        if (typeof region.n_cells === "number") {
            add("Cells", `${region.n_cells.toLocaleString("en-US")} ${region.action === "warn" ? "warned" : "flagged"}`);
        }
        return {
            kind: "region",
            key: `r:${region.roi_id}`,
            title,
            chip: {
                color: region.color || (h.categoryColor ? h.categoryColor(region.category) : null),
                words: capital(region.category_words || (h.categoryWords ? h.categoryWords(region.category) : region.category)),
            },
            status: QcHoverCard.actionStatus(region.action),
            lead, note, rows, sections: [],
            footer: "Click to open in the panel",
        };
    }

    static actionStatus(action) {
        if (action === "warn") return { words: "Warns only", tone: "warn" };
        if (action === "exclude") return { words: "Excludes cells", tone: "fail" };
        return { words: "Not applied", tone: "muted" };
    }

    // -- a cell ---------------------------------------------------------------------

    /** Whether a cell record has anything to say: a reason, a marker flag, a
     *  region, or a Segmentation QC call other than pass. */
    static hasFindings(record) {
        if (!record) return false;
        const seg = record.segqc;
        return Boolean((record.reasons || []).length || (record.markers || []).length
            || (record.regions || []).length || (seg && seg.status && seg.status !== "pass"));
    }

    /** `record` with only the reasons and marker flags whose groups the
     *  panel shows: a category's eye off silences it on the card too. */
    static visibleRecord(record, isVisible) {
        if (!record || typeof isVisible !== "function") return record;
        return Object.assign({}, record, {
            reasons: (record.reasons || []).filter((r) => isVisible("cell", r)),
            markers: (record.markers || []).filter((m) => isVisible("marker", m)),
        });
    }

    /** How a region is named on a cell's card: the user's name for it, else
     *  its subtype -- never QC's own "QC warn: segmentation error · Nucleus,
     *  AF1, ..." ROI name, which says the status and channels again. */
    static regionLabel(region) {
        const name = String((region && region.name) || "");
        if (name && !/^QC [^:]*:/.test(name)) return name;
        const words = (region && (region.class_words || QcHoverCard.words(region.class))) || "region";
        return words ? words[0].toUpperCase() + words.slice(1) : words;
    }

    /** The sentence that says why: "Low counterstain: DNA_1 4.12 below the
     *  bar 4.6 (log)", "In Tissue fold 2 · 86% inside". */
    static reasonSentence(reason) {
        const n = QcHoverCard.number;
        if (!reason) return "";
        if (String(reason.reason || "").startsWith("region:")) {
            const via = (reason.via_regions || [])[0];
            if (!via) return reason.words;
            const share = via.method === "mask" && typeof via.fraction === "number" && via.fraction < 0.995
                ? ` · ${QcHoverCard.percent(via.fraction)} inside` : "";
            return `In ${QcHoverCard.regionLabel(via)}${share}`;
        }
        if (typeof reason.value === "number" && typeof reason.cutoff === "number") {
            const where = reason.side === "low" ? "below" : "above";
            const unit = QcHoverCard.SPACE_WORDS[reason.space];
            const source = reason.source_label ? `${reason.source_label} ` : "";
            return `${reason.words}: ${source}${n(reason.value)} ${where} the bar ${n(reason.cutoff)}`
                + (unit ? ` (${unit})` : "");
        }
        if (reason.shape && Object.keys(reason.shape).length) {
            const parts = Object.entries(reason.shape)
                .map(([k, v]) => `${QcHoverCard.words(k)} ${n(v)}`);
            return `${reason.words}: ${parts.join(", ")}`;
        }
        return reason.words;
    }

    static markerLine(marker) {
        const n = QcHoverCard.number;
        const text = `${marker.marker} · ${QcHoverCard.lowerFirst(marker.words)}`;
        let detail = marker.status ? `(${marker.status})` : "";
        if (typeof marker.value === "number" && typeof marker.cutoff === "number") {
            detail = `${n(marker.value)} > ${n(marker.cutoff)} ${detail}`.trim();
        } else if ((marker.via_regions || []).length) {
            const via = marker.via_regions[0];
            detail = `in ${QcHoverCard.regionLabel(via)} ${detail}`.trim();
        }
        return { text, detail, color: marker.color };
    }

    static segqcLine(seg) {
        if (!seg || !seg.status || seg.status === "pass") return null;
        const n = QcHoverCard.number;
        const words = { under_segmented: "Merged cells", over_segmented: "Split nucleus",
                        ambiguous: "Ambiguous" }[seg.status] || QcHoverCard.words(seg.status);
        const scores = [];
        if (typeof seg.under_score === "number") scores.push(`under ${n(seg.under_score)}`);
        if (typeof seg.over_score === "number") scores.push(`over ${n(seg.over_score)}`);
        const bar = typeof seg.flag === "number" ? ` · bar ${n(seg.flag)}` : "";
        return {
            text: seg.partner_id ? `${words} · with cell ${seg.partner_id}` : words,
            detail: `${scores.join(" / ")}${bar}`,
        };
    }

    /**
     * A cell's card from its record (`/plugins/qc/cell_at`): the primary
     * reason as the lead with the value and the bar, the others under "Also",
     * the markers flagged on it, the regions it sits in, Segmentation QC's
     * call. Null for a cell QC has nothing to say about.
     */
    static cellModel(record, helpers) {
        if (!QcHoverCard.hasFindings(record)) return null;
        const h = helpers || {};
        const capital = h.capital || ((s) => String(s || ""));
        const reasons = record.reasons || [];
        const markers = record.markers || [];
        const primary = reasons[0] || null;
        const rows = [];
        const add = (label, value) => {
            if (value !== undefined && value !== null && value !== "") rows.push([label, String(value)]);
        };
        let lead = "";
        let note = "";
        let chip = null;
        if (primary) {
            lead = QcHoverCard.reasonSentence(primary);
            note = primary.notes || "";
            chip = { color: primary.color, words: capital(primary.category_words) };
            if ((primary.channels || []).length && !String(primary.reason).startsWith("region:")) {
                add("Channels", primary.channels.join(", "));
            }
            if (primary.threshold_source && (primary.threshold_source !== "auto" || primary.offset_steps)) {
                add("Bar", QcHoverCard.barSource(primary.threshold_source, primary.offset_steps));
            }
            if (primary.verdict) add("Agent", `${QcHoverCard.words(primary.verdict)} on this side`);
        } else if (markers.length) {
            chip = { color: markers[0].color, words: "Marker" };
            lead = "Kept: only some markers are unreliable here";
        } else if (record.segqc && record.segqc.status !== "pass") {
            chip = { color: null, words: "Segmentation" };
            lead = "Segmentation QC flagged this cell";
        } else {
            chip = { color: (record.regions[0] || {}).color, words: capital((record.regions[0] || {}).category_words) };
            lead = "In a QC region that does not flag it";
        }
        const named = new Set((primary && primary.via_regions || []).map((r) => r.roi_id));
        const sections = [];
        const others = reasons.slice(1);
        if (others.length) {
            const cap = QcHoverCard.MAX_ITEMS;
            const items = others.slice(0, cap).map((r) => ({
                text: QcHoverCard.reasonSentence(r),
                detail: r.status === "fail" ? "excluded" : "warned",
                color: r.color,
            }));
            if (others.length > cap) items.push({ text: `+${others.length - cap} more in the panel`, more: true });
            for (const r of others) for (const via of r.via_regions || []) named.add(via.roi_id);
            sections.push({ heading: "Also", items });
        }
        if (markers.length) {
            const cap = QcHoverCard.MAX_ITEMS;
            const items = markers.slice(0, cap).map((m) => QcHoverCard.markerLine(m));
            if (markers.length > cap) items.push({ text: `+${markers.length - cap} more in the panel`, more: true });
            sections.push({ heading: "Markers", items });
        }
        const regions = (record.regions || []).filter((r) => !named.has(r.roi_id));
        if (regions.length) {
            sections.push({
                heading: "In regions",
                items: regions.slice(0, QcHoverCard.MAX_ITEMS).map((r) => ({
                    text: QcHoverCard.regionLabel(r),
                    detail: [QcHoverCard.words(r.action),
                             r.method === "mask" && typeof r.fraction === "number"
                                 ? `${QcHoverCard.percent(r.fraction)} inside` : ""]
                        .filter(Boolean).join(" · "),
                    color: r.color,
                })),
            });
        }
        const seg = QcHoverCard.segqcLine(record.segqc);
        if (seg && !(primary && String(primary.reason).startsWith("seg_"))) {
            sections.push({ heading: "Segmentation QC", items: [seg] });
        } else if (seg) {
            add("Segmentation QC", `${seg.text} · ${seg.detail}`);
        }
        return {
            kind: "cell",
            key: `c:${record.cell_id}`,
            title: `Cell ${record.cell_id}`,
            chip,
            status: QcHoverCard.cellStatus(record),
            lead, note, rows, sections,
            footer: "",
        };
    }

    static cellStatus(record) {
        if (record.action === "exclude") return { words: "Excluded", tone: "fail" };
        if (record.action === "warn") return { words: "Warned", tone: "warn" };
        if ((record.unreliable_markers || []).length) return { words: "Marker unreliable", tone: "warn" };
        if ((record.markers || []).length) return { words: "Marker flagged", tone: "warn" };
        return { words: "Kept", tone: "pass" };
    }

    /**
     * A cell's card from the panel's own groups, for when the server has no
     * current calls to read (`calls: false`) but the cell layer still lists
     * the cell: what it is coloured for, without the numbers.
     */
    static cellModelFromGroups(cellId, groups, helpers, note) {
        const h = helpers || {};
        const capital = h.capital || ((s) => String(s || ""));
        const whole = (groups || []).filter((g) => g.level === "cell")
            .sort((a, b) => (a.status === "fail" ? 0 : 1) - (b.status === "fail" ? 0 : 1));
        const markers = (groups || []).filter((g) => g.level === "marker");
        const seg = (groups || []).filter((g) => g.level === "segqc");
        if (!whole.length && !markers.length && !seg.length) return null;
        const first = whole[0] || markers[0] || seg[0];
        const sections = [];
        if (whole.length > 1) {
            sections.push({
                heading: "Also",
                items: whole.slice(1, 1 + QcHoverCard.MAX_ITEMS).map((g) => ({
                    text: g.label, detail: g.status === "fail" ? "excluded" : "warned", color: g.color,
                })),
            });
        }
        const markerItems = (whole.length ? markers : markers.slice(1)).slice(0, QcHoverCard.MAX_ITEMS)
            .map((g) => ({ text: g.label, detail: g.status ? `(${g.status})` : "", color: g.color }));
        if (markerItems.length) sections.push({ heading: "Markers", items: markerItems });
        const segItems = (first === seg[0] ? seg.slice(1) : seg)
            .map((g) => ({ text: g.label, color: g.color }));
        if (segItems.length) sections.push({ heading: "Segmentation QC", items: segItems });
        const status = whole.length
            ? (whole[0].status === "fail" ? { words: "Excluded", tone: "fail" } : { words: "Warned", tone: "warn" })
            : { words: "Flagged", tone: "warn" };
        return {
            kind: "cell",
            key: `c:${cellId}`,
            title: `Cell ${cellId}`,
            chip: { color: first.color, words: capital(first.category_words || first.label) },
            status,
            lead: first.label,
            note: note || "",
            rows: [], sections,
            footer: "",
        };
    }
}

class QcHoverProbe {

    /** How long the pointer rests before the server is asked about a cell. */
    static get CELL_DELAY_MS() { return 120; }
    /** Failures in a row before cell questions pause, and for how long. */
    static get MAX_FAILURES() { return 3; }
    static get PAUSE_MS() { return 10000; }

    /**
     * @param {object} ctx - the plugin context (`ctx.viewer.viewer` is OSD).
     * @param {object} deps - `{overlay, api, toImage, imagePerScreen,
     *   isSuppressed, isCellLayerOn, isFindingVisible, helpers, cellGroupsFor,
     *   onSelect}`.
     */
    constructor(ctx, deps) {
        this.ctx = ctx;
        this.deps = deps || {};
        this.card = new QcHoverCard();
        this.tracker = null;
        this.viewer = null;
        this._handlers = [];
        this._offViewport = null;
        this.position = null;     // the pointer, in canvas pixels
        this.point = null;        // ... and in full-resolution image pixels
        this.region = null;
        this.regionModel = null;
        this.cellModel = null;
        this.cellPoint = null;    // where the last cell question was asked
        this.token = 0;
        this.failures = 0;
        this.pausedUntil = 0;
        this._frame = 0;
        this._timer = 0;
        this._warned = false;
    }

    get armed() {
        return Boolean(this.tracker);
    }

    arm() {
        if (this.tracker) return true;
        const viewer = this.ctx?.viewer?.viewer;
        const OSD = window.OpenSeadragon;
        if (!viewer || !viewer.canvas || !OSD || !OSD.MouseTracker) return false;
        this.viewer = viewer;
        // A tracker of its own, as roiTools has: OSD's canvas-* events have no
        // plain move, and a hover is an observation, not a gesture.
        this.tracker = new OSD.MouseTracker({
            element: viewer.canvas,
            moveHandler: (event) => this.pointerMove(event),
            leaveHandler: () => this.clear(),
        });
        const on = (name, fn) => {
            viewer.addHandler(name, fn);
            this._handlers.push([name, fn]);
        };
        on("canvas-press", () => this.clear({ keepPosition: true }));
        on("canvas-drag", () => this.clear());
        on("canvas-scroll", () => this.clear({ keepPosition: true }));
        on("canvas-click", (event) => this.click(event));
        this._offViewport = this.ctx.layers?.onViewportChange?.(() => this.viewportMoved()) || null;
        return true;
    }

    disarm() {
        this.clear();
        this.tracker?.destroy?.();
        this.tracker = null;
        for (const [name, fn] of this._handlers) this.viewer?.removeHandler?.(name, fn);
        this._handlers = [];
        this._offViewport?.();
        this._offViewport = null;
        this.viewer = null;
    }

    destroy() {
        this.disarm();
        this.card.destroy();
    }

    suppressed() {
        try {
            return Boolean(this.deps.isSuppressed && this.deps.isSuppressed());
        } catch (error) {
            return true;
        }
    }

    pointerMove(event) {
        if (!this.tracker) return;
        if (this.suppressed()) {
            this.clear();
            return;
        }
        if (!event || !event.position) return;
        // Kept as OSD's own Point: the viewer's pixel-to-image conversion calls
        // its methods (`minus`), and a plain {x, y} throws there.
        this.position = typeof event.position.clone === "function"
            ? event.position.clone() : event.position;
        // One hit test a frame: moves come far faster than the card can change.
        if (this._frame) return;
        this._frame = requestAnimationFrame(() => {
            this._frame = 0;
            this.resolve();
        });
    }

    /** What is under the pointer now: the region at once, the cell after a
     *  rest. */
    resolve() {
        if (!this.tracker || !this.position) return;
        if (this.suppressed()) {
            this.clear();
            return;
        }
        const point = this.deps.toImage ? this.deps.toImage(this.position) : null;
        if (!point) return;
        const [x, y] = point;
        this.point = { x, y };
        const scale = this.imagePerScreen();
        const hit = this.deps.overlay?.hitTest?.(x, y, { tolerance: scale * 3 }) || null;
        const region = hit ? hit.region : null;
        // Moving about inside one region is one hover, not one a frame.
        if (region !== this.region) {
            this.region = region;
            this.regionModel = region
                ? QcHoverCard.regionModel(region, this.deps.helpers) : null;
        }
        if (this.cellLayerOn()) this.askCell(x, y, scale);
        else this.dropCell();
        this.render();
    }

    imagePerScreen() {
        try {
            const value = Number(this.deps.imagePerScreen ? this.deps.imagePerScreen() : 1);
            return value > 0 && Number.isFinite(value) ? value : 1;
        } catch (error) {
            return 1;
        }
    }

    cellLayerOn() {
        try {
            return Boolean(this.deps.isCellLayerOn && this.deps.isCellLayerOn());
        } catch (error) {
            return false;
        }
    }

    /** Ask the server for the cell at (x, y) once the pointer rests. A
     *  pointer that has barely moved since the last question is not asked
     *  about again; an answer to an older question is dropped. */
    askCell(x, y, scale) {
        if (Date.now() < this.pausedUntil) return;
        if (this.cellPoint && Math.hypot(x - this.cellPoint.x, y - this.cellPoint.y) < scale * 2) return;
        window.clearTimeout(this._timer);
        const token = ++this.token;
        const radius = Math.min(64, Math.max(1, scale * 5));
        this._timer = window.setTimeout(() => {
            this._timer = 0;
            this.fetchCell(token, x, y, radius);
        }, QcHoverProbe.CELL_DELAY_MS);
    }

    async fetchCell(token, x, y, radius) {
        let answer = null;
        try {
            answer = await this.deps.api.cellAt(x, y, radius);
        } catch (error) {
            answer = null;
        }
        if (token !== this.token || !this.tracker) return;
        if (!answer || !answer.ok) {
            this.failures += 1;
            if (this.failures >= QcHoverProbe.MAX_FAILURES) {
                this.pausedUntil = Date.now() + QcHoverProbe.PAUSE_MS;
                this.failures = 0;
                if (!this._warned) {
                    this._warned = true;
                    console.warn("QC hover: the cell under the pointer could not be read; "
                        + "cell cards pause for a few seconds.");
                }
            }
            return;
        }
        this.failures = 0;
        this.cellPoint = { x, y };
        const record = (answer.data || {}).cell || null;
        let model = null;
        if (record && record.calls === false) {
            const groups = this.deps.cellGroupsFor ? this.deps.cellGroupsFor(record.cell_id) : [];
            model = QcHoverCard.cellModelFromGroups(record.cell_id, groups, this.deps.helpers);
        } else if (record) {
            model = QcHoverCard.cellModel(
                QcHoverCard.visibleRecord(record, this.deps.isFindingVisible), this.deps.helpers);
        }
        this.cellModel = model;
        if (model && this.region) model.footer = "Click to open the region in the panel";
        this.render();
    }

    dropCell() {
        window.clearTimeout(this._timer);
        this._timer = 0;
        this.token += 1;
        this.cellModel = null;
        this.cellPoint = null;
    }

    anchor() {
        const box = this.viewer?.canvas?.getBoundingClientRect?.();
        if (!box || !this.position) return null;
        return { x: box.left + this.position.x, y: box.top + this.position.y };
    }

    bounds() {
        const box = this.viewer?.canvas?.getBoundingClientRect?.();
        return box ? { left: box.left, top: box.top, right: box.right, bottom: box.bottom } : null;
    }

    render() {
        const model = this.cellModel || this.regionModel;
        const anchor = this.anchor();
        if (!model || !anchor) {
            this.card.hide();
            return;
        }
        this.card.show(model, anchor, this.bounds());
    }

    /** The pointer is busy or gone: nothing is described. `keepPosition`
     *  leaves the pointer where it is, so a click can still be read. */
    clear(options) {
        if (this._frame) {
            cancelAnimationFrame(this._frame);
            this._frame = 0;
        }
        window.clearTimeout(this._timer);
        this._timer = 0;
        this.token += 1;
        if (!(options && options.keepPosition)) this.position = null;
        this.region = null;
        this.regionModel = null;
        this.cellModel = null;
        this.cellPoint = null;
        this.card.hide();
    }

    /** A drag or a stroke has begun (qcDraw / the controller). */
    gesture() {
        this.clear();
    }

    /** A plain click on a region opens it in the panel. */
    click(event) {
        if (!this.tracker || !event || event.quick === false) return;
        if (this.suppressed() || !event.position) return;
        const point = this.deps.toImage ? this.deps.toImage(event.position) : null;
        if (!point) return;
        const hit = this.deps.overlay?.hitTest?.(point[0], point[1],
            { tolerance: this.imagePerScreen() * 3 });
        if (!hit) return;
        this.clear();
        this.deps.onSelect?.(hit.region);
    }

    /** The picture moved under a pointer that did not: what is under it is
     *  asked again (a zoom slides shapes out from under it). */
    viewportMoved() {
        if (!this.position || !this.card.visible || this._frame) return;
        this.cellPoint = null;
        this._frame = requestAnimationFrame(() => {
            this._frame = 0;
            this.resolve();
        });
    }

    /** The regions or the cells were reloaded under a still pointer: a region
     *  that has gone or been hidden is let go, and a cell is asked again. */
    revalidate() {
        if (!this.position) return;
        this.cellModel = null;
        this.cellPoint = null;
        this.region = null;
        this.regionModel = null;
        if (this._frame) return;
        this._frame = requestAnimationFrame(() => {
            this._frame = 0;
            this.resolve();
        });
    }
}

window.QcHoverCard = QcHoverCard;
window.QcHoverProbe = QcHoverProbe;
