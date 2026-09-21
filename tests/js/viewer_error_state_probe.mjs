/**
 * Why the canvas is empty, said on the canvas.
 *
 * The decisions here, and what each costs when wrong:
 *
 *   - THREE CAUSES, THREE SENTENCES. A file that moved, a file this process
 *     may not read and a file whose bytes are not an image want three
 *     different actions. "Could not load" covers all three and helps with none.
 *   - `unavailable` IS NOT ONE OF THEM. A layer on a machine that is asleep
 *     already has a banner, and that banner can offer to connect the machine.
 *     Drawing this card over it would replace an answer with a statement.
 *   - THE REST OF THE PAGE STAYS LIVE. Only the card takes pointer events, so
 *     the dataset's Prev/Next -- the most likely next thing somebody wants --
 *     keeps working over the top of it.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/viewerErrorState.js");

let checks = 0;
function check(label, fn) {
    fn();
    checks += 1;
    console.log("  ok  " + label);
}

function element(tag = "div") {
    const node = {
        tagName: tag.toUpperCase(),
        className: "",
        title: "",
        href: "",
        textContent: "",
        children: [],
        attributes: {},
        appendChild(child) {
            this.children.push(child);
            child.parentNode = this;
            return child;
        },
        removeChild(child) {
            this.children = this.children.filter((c) => c !== child);
            child.parentNode = null;
        },
        setAttribute(name, value) { this.attributes[name] = value; },
    };
    node.parentNode = null;
    return node;
}

function boot({ wrapper = true, datasource = "sampleB", datasetId = null } = {}) {
    const mount = wrapper ? element("div") : null;
    const context = {
        console: { log() {}, error() {}, warn() {} },
        Boolean, String, Object, Array,
        encodeURIComponent,
        document: {
            getElementById: (id) => (id === "openseadragon_wrapper" ? mount : null),
            createElement: element,
        },
        plexoraUrl: (path) => "/" + String(path || "").replace(/^\/+/, ""),
    };
    context.window = context;
    context.flaskVariables = { datasource };
    context.PlexoraDatasetNav = { datasetId: () => datasetId };
    createContext(context);
    runInContext(readFileSync(SOURCE, "utf8"), context);
    return { api: context.PlexoraViewerError, mount };
}

function textOf(node) {
    return (node.textContent || "")
        + (node.children || []).map(textOf).join(" ");
}

function find(node, predicate) {
    if (!node) return null;
    if (predicate(node)) return node;
    for (const child of node.children || []) {
        const hit = find(child, predicate);
        if (hit) return hit;
    }
    return null;
}

console.log("viewer error state");

check("a missing file says the file is not there", () => {
    const t = boot();
    assert.equal(t.api.show({ status: "missing", src: "/data/slide.tif" }), true);
    const text = textOf(t.mount);
    assert.match(text, /could not be loaded/i);
    assert.match(text, /moved, renamed or deleted/);
});

check("an unreadable file says it is a permission problem", () => {
    const t = boot();
    t.api.show({ status: "inaccessible", src: "/data/slide.tif" });
    assert.match(textOf(t.mount), /not allowed to read it/);
});

check("a corrupt file says the bytes are the problem", () => {
    const t = boot();
    t.api.show({ status: "corrupt", src: "/data/slide.tif" });
    assert.match(textOf(t.mount), /could not be opened as an image/);
});

check("the three sentences are all different", () => {
    const t = boot();
    const said = ["missing", "inaccessible", "corrupt"].map((status) => {
        t.api.show({ status });
        return textOf(t.mount);
    });
    assert.equal(new Set(said).size, 3, "a shared sentence helps with nothing");
});

check("an unreachable node is left to the banner that can fix it", () => {
    const t = boot();
    assert.equal(t.api.show({ status: "unavailable", detail: "node asleep" }), false);
    assert.equal(t.api.isShowing(), false);
    assert.equal(t.mount.children.length, 0);
});

check("a healthy image draws nothing", () => {
    const t = boot();
    assert.equal(t.api.show({ status: "ok" }), false);
    assert.equal(t.mount.children.length, 0);
});

check("an unknown status draws nothing rather than guessing", () => {
    const t = boot();
    assert.equal(t.api.show({ status: "sideways" }), false);
    assert.equal(t.api.show({}), false);
    assert.equal(t.api.show(null), false);
});

check("the file is named, because somebody has to go and find it", () => {
    const t = boot();
    const src = "/mnt/share/run7/slide.ome.tif";
    t.api.show({ status: "missing", src });
    const path = find(t.mount, (n) => n.className === "viewer-error-path");
    assert.equal(path.textContent, src);
    assert.equal(path.title, src, "and it is not truncated out of reach");
});

check("the server's own line is shown, so two failures read differently", () => {
    const t = boot();
    t.api.show({ status: "corrupt", detail: "not a TIFF file: header=b''" });
    assert.match(textOf(t.mount), /not a TIFF file/);
});

check("it offers the edit page, where the image field is", () => {
    const t = boot({ datasource: "sampleB" });
    t.api.show({ status: "missing", src: "/x" });
    const edit = find(t.mount, (n) => n.tagName === "A"
        && /Repoint/.test(n.textContent));
    assert.match(edit.href, /edit_config\/sampleB/);
});

check("and the way back to this sample's own dataset when it has one", () => {
    const t = boot({ datasetId: "ds7" });
    t.api.show({ status: "missing", src: "/x" });
    const back = find(t.mount, (n) => n.tagName === "A"
        && /Back to samples/.test(n.textContent));
    assert.match(back.href, /open_project\?dataset=ds7/);
});

check("falling back to the whole list when it is in none", () => {
    const t = boot({ datasetId: null });
    t.api.show({ status: "missing", src: "/x" });
    const back = find(t.mount, (n) => n.tagName === "A"
        && /Back to samples/.test(n.textContent));
    assert.equal(back.href, "/open_project");
});

check("it announces itself", () => {
    const t = boot();
    t.api.show({ status: "missing", src: "/x" });
    const card = t.mount.children[0];
    assert.equal(card.attributes.role, "alert");
});

check("showing twice leaves one card, not two", () => {
    const t = boot();
    t.api.show({ status: "missing", src: "/x" });
    t.api.show({ status: "corrupt", src: "/x" });
    assert.equal(t.mount.children.length, 1);
    assert.match(textOf(t.mount), /could not be opened as an image/);
});

check("hiding takes it off the page and off the record", () => {
    const t = boot();
    t.api.show({ status: "missing", src: "/x" });
    assert.equal(t.api.isShowing(), true);
    t.api.hide();
    assert.equal(t.api.isShowing(), false);
    assert.equal(t.mount.children.length, 0);
});

check("a page with no canvas is left alone", () => {
    const t = boot({ wrapper: false });
    assert.equal(t.api.show({ status: "missing", src: "/x" }), false);
});

console.log(`\n${checks} checks passed`);
