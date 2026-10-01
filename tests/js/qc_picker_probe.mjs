/**
 * The QC panel's five categories: the picker, its help, the tree's grouping
 * and a region's details.
 *
 * What is worth pinning is where the categories could drift from the server
 * or from each other: a picker that lists the classes again instead of the
 * five, a subtype typed into Custom that loses its class, a help that opens
 * by default or closes the menu, a region grouped by its class, the cells of
 * a category that an eye does not hide, a Details popup that leaves out the
 * threshold's source, and downloads that miss the provenance.
 *
 * The real QcTree and QcSidebarController (their prototypes; no viewer), in
 * a vm with a hand-built document.
 *
 * Run directly: `node tests/js/qc_picker_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora", "plugins", "qc", "static");

function classList() {
    const set = new Set();
    return {
        add: (...names) => names.forEach((c) => set.add(c)), remove: (c) => set.delete(c),
        contains: (c) => set.has(c), has: (c) => set.has(c),
        toggle: (c, on) => { const want = on === undefined ? !set.has(c) : Boolean(on);
                             if (want) set.add(c); else set.delete(c); return want; },
    };
}

function element(tag = "div") {
    const node = {
        tagName: tag.toUpperCase(), children: [], parent: null, attributes: {}, dataset: {},
        style: { props: {}, setProperty(k, v) { this.props[k] = v; } },
        hidden: false, disabled: false, textContent: "", title: "", className: "",
        classList: classList(), listeners: {}, offsetWidth: 240, offsetHeight: 300,
        setAttribute(key, value) { node.attributes[key] = String(value); },
        getAttribute(key) { return node.attributes[key] ?? null; },
        addEventListener(name, fn) { (node.listeners[name] ||= []).push(fn); },
        dispatch(name, event = {}) {
            const e = { currentTarget: node, target: node, stopPropagation() { e.stopped = true; },
                        preventDefault() {}, ...event };
            for (const fn of node.listeners[name] || []) fn(e);
            return e;
        },
        click() { return node.dispatch("click"); },
        focus() {},
        getBoundingClientRect() { return { left: 10, right: 40, top: 10, bottom: 30 }; },
        appendChild(child) { child.parent = node; node.children.push(child); return child; },
        append(...kids) { kids.forEach((k) => node.appendChild(k)); },
        remove() { node.parent?.children.splice(node.parent.children.indexOf(node), 1);
                   node.parent = null; },
        querySelector(selector) { return find(node, selector); },
        contains(other) { return all(node).includes(other); },
    };
    return node;
}

function all(node) {
    return [node, ...node.children.flatMap(all)];
}

function has(node, name) {
    return String(node.className || "").split(/\s+/).includes(name);
}

function find(node, selector) {
    const name = selector.replace(/^\./, "").split(":")[0];
    return all(node).slice(1).find((n) => has(n, name) && !n.disabled) || null;
}

const listeners = {};
const document = {
    createElement: (tag) => element(tag),
    body: element("body"),
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    removeEventListener() {},
    querySelectorAll: () => [],
    getElementById: () => null,
};
const window = { innerWidth: 1200, innerHeight: 900, setTimeout: (fn) => 0,
                 clearTimeout() {}, addEventListener() {}, removeEventListener() {} };
const context = createContext({ window, document, console, setTimeout: () => 0,
                                clearTimeout() {}, navigator: {} });
window.window = window;
for (const file of ["qcTree.js", "qcSidebarController.js"]) {
    runInContext(readFileSync(join(STATIC, file), "utf8"), context, { filename: file });
}
const QcTree = window.QcTree;
const Controller = window.QcSidebarController;

const VOCABULARY = {
    categories: [
        { id: "blur_focus", words: "Blur / focus issue", color: "#f97316",
          default_class: "out_of_focus", help: "out-of-focus fields, blur in one channel" },
        { id: "registration", words: "Registration issue", color: "#3b82f6",
          default_class: "cross_cycle_registration_error", help: "a cycle offset" },
        { id: "segmentation", words: "Segmentation issue", color: "#a855f7",
          default_class: "segmentation_error", help: "merged cells, split nuclei" },
        { id: "tissue_acquisition", words: "Tissue / acquisition artifact", color: "#eab308",
          default_class: "tissue_artifact", help: "folds, tears, tile seams" },
        { id: "staining_signal", words: "Staining / signal artifact", color: "#22c55e",
          default_class: "staining_artifact", help: "antibody aggregates, failed channels" },
    ],
    review: { id: "review", words: "Needs review", color: "#94a3b8" },
    classes: [{ id: "tissue_fold", words: "tissue fold", category: "tissue_acquisition" },
              { id: "out_of_focus", words: "out of focus", category: "blur_focus" },
              { id: "uncertain_manual_review", words: "manual review", category: "review" }],
    custom: [{ id: "custom_pen_mark", words: "Pen mark", color: "#e879f9" }],
};

function controller() {
    const c = Object.create(Controller.prototype);
    c.vocabulary = VOCABULARY;
    c.hidden = new Set();
    c.roiMuted = false;
    c.selectedRegion = null;
    c.state = { summary: { regions: {} }, provenance: { result_id: "qr", session_id: null } };
    c.regionData = { regions: [] };
    c.cellData = null;
    c.drawn = [];
    c.startDrawing = (target) => { c.drawn.push(target); return Promise.resolve(true); };
    c.message = () => {};
    return c;
}

function items(menu) {
    return menu.children.filter((n) => has(n, "qc-menu-item"));
}

function text(node) {
    return all(node).map((n) => n.textContent).filter(Boolean).join(" ");
}

/** Equal values across the vm's realm (its objects have other prototypes). */
function same(actual, expected) {
    assert.equal(JSON.stringify(actual), JSON.stringify(expected));
}

function check(name, fn) {
    fn();
    console.log(`ok  ${name}`);
}

check("the picker lists Custom, then the five categories in order, each in its colour", () => {
    const c = controller();
    const anchor = element("button");
    c.openDrawMenu(anchor);
    const menu = QcTree._popup.el;
    assert.ok(has(menu, "qc-picker--categories"));
    const custom = menu.children.find((n) => has(n, "qc-picker-custom"));
    const buttons = items(menu);
    assert.ok(menu.children.indexOf(custom) < menu.children.indexOf(buttons[0]));
    const labels = buttons.map((b) => text(b));
    same(labels.slice(0, 5), VOCABULARY.categories.map((x) => x.words));
    assert.equal(labels[5], "Pen mark");
    assert.ok(!labels.includes("tissue fold") && !labels.includes("Tissue fold"));
    const dots = buttons.slice(0, 5).map((b) => b.children.find((n) => has(n, "qc-menu-dot")));
    same(dots.map((d) => d.style.props["--qc-row-color"]),
                     VOCABULARY.categories.map((x) => x.color));
    QcTree.closePopup();
});

check("a category draws in that category", () => {
    const c = controller();
    c.openDrawMenu(element("button"));
    items(QcTree._popup.el)[3].click();
    same(c.drawn, [{ category: "tissue_acquisition" }]);
});

check("a subtype typed into Custom draws its class; a category's words the category; others a custom one", () => {
    const { categories, classes } = VOCABULARY;
    same(Controller.pickerTarget("Tissue fold", categories, classes),
                     { class: "tissue_fold" });
    same(Controller.pickerTarget("registration issue", categories, classes),
                     { category: "registration" });
    same(Controller.pickerTarget("Pen mark", categories, classes),
                     { label: "Pen mark" });
    const c = controller();
    c.openDrawMenu(element("button"));
    const input = all(QcTree._popup.el).find((n) => has(n, "qc-picker-input"));
    input.value = "  tissue   fold ";
    input.dispatch("keydown", { key: "Enter" });
    same(c.drawn, [{ class: "tissue_fold" }]);
});

check("the ? help is folded away until opened, and opening it does not close the menu", () => {
    const c = controller();
    c.openDrawMenu(element("button"));
    const menu = QcTree._popup.el;
    const heading = menu.children.find((n) => has(n, "qc-menu-heading"));
    const action = heading.children.find((n) => has(n, "qc-menu-heading-action"));
    const icon = action.children[0];
    assert.ok(has(icon, "fa-circle-question"));
    const help = menu.children.find((n) => has(n, "qc-picker-help"));
    assert.equal(help.hidden, true);
    assert.equal(action.getAttribute("aria-expanded"), "false");
    const event = action.click();
    assert.ok(event.stopped);
    assert.equal(help.hidden, false);
    assert.equal(action.getAttribute("aria-expanded"), "true");
    assert.equal(QcTree._popup.el, menu);
    action.click();
    assert.equal(help.hidden, true);
    QcTree.closePopup();
});

check("each help row unfolds to its one line of what it groups", () => {
    const help = Controller.pickerHelp(VOCABULARY.categories);
    assert.equal(help.children.length, 5);
    const entry = help.children[3];
    const [row, groups] = entry.children;
    assert.equal(groups.hidden, true);
    assert.equal(groups.textContent, "folds, tears, tile seams");
    assert.ok(text(row).includes("Tissue / acquisition artifact"));
    row.click();
    assert.equal(groups.hidden, false);
    assert.equal(row.getAttribute("aria-expanded"), "true");
    assert.equal(help.children[0].children[1].hidden, true);
});

check("regions are grouped by category in the five's order, the subtype noted", () => {
    const c = controller();
    c.regionData = { regions: [
        { roi_id: "a", category: "tissue_acquisition", category_words: "Tissue / acquisition artifact",
          class: "tissue_fold", words: "tissue fold", action: "exclude", created_by: "agent",
          color: "#eab308", channels: [] },
        { roi_id: "b", category: "blur_focus", category_words: "Blur / focus issue",
          class: "out_of_focus", words: "out of focus", action: "exclude", created_by: "blur",
          color: "#f97316", channels: ["DNA_2"] },
        { roi_id: "c", category: "tissue_acquisition", category_words: "Tissue / acquisition artifact",
          class: "tissue_artifact", words: "tissue or acquisition artifact", action: "warn",
          created_by: "user", color: "#eab308", channels: [] },
    ] };
    const specs = c.specs();
    const groups = specs.filter((s) => s.kind === "class").map((s) => s.label);
    same(groups, ["Blur / focus issue", "Tissue / acquisition artifact"]);
    const rows = Object.fromEntries(specs.filter((s) => s.kind === "region")
        .map((s) => [s.ref.roi_id, s.note]));
    assert.equal(rows.a, "tissue fold · AI");
    assert.equal(rows.b, "Derived from Blur QC");
    assert.equal(rows.c, "manual");
});

check("cells sit under their category, whose eye hides its reasons", () => {
    const c = controller();
    c.cellData = { available: true, n: 100, n_fail: 8, n_warn: 2, groups: [
        { key: "region:tissue_fold|fail", reason: "region:tissue_fold", status: "fail",
          level: "cell", category: "tissue_acquisition",
          category_words: "Tissue / acquisition artifact", label: "In tissue fold", count: 5 },
        { key: "seg_under|fail", reason: "seg_under", status: "fail", level: "cell",
          category: "segmentation", category_words: "Segmentation issue",
          label: "Merged cells", count: 3 },
        { key: "seg_irregular|warn", reason: "seg_irregular", status: "warn", level: "cell",
          category: "segmentation", category_words: "Segmentation issue",
          label: "Irregular shape", count: 2 },
    ] };
    const specs = c.specs();
    const cats = specs.filter((s) => s.kind === "cellcat");
    same(cats.map((s) => s.label), ["Segmentation issue",
                                                "Tissue / acquisition artifact"]);
    assert.equal(cats[0].count, 5);
    const reasons = specs.filter((s) => s.kind === "reason");
    assert.ok(reasons.every((s) => s.level === 2));
    c.hidden.add("q:segmentation");
    const merged = c.cellData.groups[1];
    assert.equal(c.cellGroupVisible(merged), false);
    assert.equal(c.cellGroupVisible(c.cellData.groups[0]), true);
});

check("the details popup lists the provenance, the threshold's source included", () => {
    const c = controller();
    const region = { roi_id: "qcroi_1", name: "QC exclude: out of focus · DNA_2",
                     category: "blur_focus", category_words: "Blur / focus issue",
                     class: "out_of_focus", words: "out of focus", action: "exclude",
                     created_by: "blur", channels: ["DNA_2"], cycles: [2],
                     tool: { name: "blur", version: "1" }, score: 0.71, score_kind: "blur_score",
                     threshold: 0.35, threshold_source: "agent_refined", offset_steps: 1,
                     ai: { verdict: "artifact", confidence: "sure", source: "score_review" },
                     refinement: { status: "refined", method: "blur", kept_fraction: 0.6 },
                     n_cells: 42 };
    const anchor = element("button");
    c.showDetails(region, anchor);
    const box = QcTree._popup.el;
    assert.ok(has(box, "qc-details"));
    const list = box.children.find((n) => n.tagName === "DL");
    const facts = {};
    for (let i = 0; i < list.children.length; i += 2) {
        facts[list.children[i].textContent] = list.children[i + 1].textContent;
    }
    assert.equal(facts.Category, "Blur / focus issue");
    assert.equal(facts.Subtype, "Out of focus");
    assert.equal(facts["Found by"], "blur v1");
    assert.equal(facts.Threshold, "0.35 (agent refined, +1 step)");
    assert.equal(facts.Agent, "artifact (sure) · score review");
    assert.equal(facts["Cells derived"], "42");
    assert.equal(facts.ROI, "qcroi_1");
    assert.ok(c.regionDetails(region).includes("threshold: 0.35 (agent refined, +1 step)"));
    const menu = c.menuFor({ kind: "region", key: "r:qcroi_1", ref: region }, anchor);
    assert.ok(menu.some((item) => item.label === "Details"));
    QcTree.closePopup();
});

check("the download menu offers the provenance and the findings", () => {
    const c = controller();
    c.cellData = { available: true };
    c.download = (kind) => c.drawn.push(kind);
    c.openDownloadMenu(element("button"));
    const labels = items(QcTree._popup.el).map((b) => text(b));
    assert.ok(labels.includes("Provenance (JSON)") && labels.includes("Findings (CSV)"));
    items(QcTree._popup.el).find((b) => text(b) === "Findings (CSV)").click();
    same(c.drawn, ["findings.csv"]);
});
