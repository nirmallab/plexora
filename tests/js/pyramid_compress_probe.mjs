/**
 * The Import dialog's pyramid question and its "Compress the copy" switch
 * (views/importSample.js `askPyramid`), run from source against a hand-made
 * DOM.
 *
 * What has to hold:
 *
 *   - **The switch is there only where the copy can be compressed** (the
 *     server says `compressible`: a 16-bit stack, a Python that makes
 *     compressed copies), and it starts where the setting is
 *     (`compress_default`) or where an earlier answer left it.
 *   - **Flipping it says the other question at once, in the same dialog** --
 *     title, body (with or without the compression clause) and the chip's
 *     size -- and both bodies say the original is not changed.
 *   - **The answer carries the choice**: "compressed" or "exact" for
 *     "Pyramidize and import", "no" for "Import as is", null for the X; a
 *     question with no switch answers "yes" as before.
 *   - **The row's note and the website's folder question** name a compressed
 *     copy, and say the original is not changed.
 *
 * The functions are taken out of importSample.js by name (it is one IIFE),
 * with the few helpers they call stood in for.
 *
 * Run directly:  node tests/js/pyramid_compress_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/views/importSample.js"), "utf8");

let failures = 0;
function check(label, condition, detail) {
    if (condition) {
        console.log(`ok - ${label}`);
    } else {
        failures += 1;
        console.log(`FAIL - ${label}${detail === undefined ? "" : ` (${JSON.stringify(detail)})`}`);
    }
}

/** `function NAME(...) {...}` out of the source, braces matched. */
function lift(name) {
    const start = SOURCE.search(new RegExp(`(async )?function ${name}\\(`));
    if (start < 0) throw new Error(`no function ${name} in importSample.js`);
    let depth = 0;
    for (let i = SOURCE.indexOf("{", SOURCE.indexOf(")", start)); i < SOURCE.length; i += 1) {
        if (SOURCE[i] === "{") depth += 1;
        if (SOURCE[i] === "}") {
            depth -= 1;
            if (depth === 0) return SOURCE.slice(start, i + 1);
        }
    }
    throw new Error(`unbalanced function ${name}`);
}

/* ------------------------------------------------------------------ DOM -- */

class Node_ {
    constructor(tag, className = "", text = "") {
        this.tagName = String(tag).toUpperCase();
        this.className = className || "";
        this.textContent = text || "";
        this.children = [];
        this.parent = null;
        this.attrs = {};
        this.listeners = {};
        this.checked = false;
        this.type = "";
    }
    appendChild(child) {
        child.parent = this;
        this.children.push(child);
        return child;
    }
    append(...children) { children.forEach((child) => this.appendChild(child)); }
    replaceWith(other) {
        const siblings = this.parent.children;
        siblings[siblings.indexOf(this)] = other;
        other.parent = this.parent;
        this.parent = null;
    }
    setAttribute(name, value) { this.attrs[name] = String(value); }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    fire(type) { (this.listeners[type] || []).forEach((fn) => fn({ type, target: this })); }
    closest(selector) {
        for (let node = this; node; node = node.parent) {
            if (selector === "dialog" && node.tagName === "DIALOG") return node;
        }
        return null;
    }
    all() { return [this, ...this.children.flatMap((child) => child.all())]; }
    querySelector(selector) {
        const cls = selector.replace(/^\./, "");
        return this.all().slice(1).find((node) => node.className.split(/\s+/).includes(cls)) || null;
    }
    text() { return [this.textContent, ...this.children.map((child) => child.text())].join(" ").trim(); }
}

/* -------------------------------------------------------------- the page -- */

function world({ pyramids = null, store = null } = {}) {
    const asked = [];
    const toasts = [];
    const window = {
        PlexoraToast: { show: (toast) => toasts.push(toast) },
        parent: { PlexoraStore: store },
        PlexoraConfirm: {
            // A dialog as PlexoraConfirm builds one: title, body, the content,
            // then the answers. Settled by the probe, as a click would.
            choose(options) {
                const dialog = new Node_("dialog", "plx-dialog plx-confirm");
                dialog.appendChild(new Node_("h2", "plx-dialog-title", options.title));
                dialog.appendChild(new Node_("p", "plx-confirm-body", options.body));
                if (options.content) dialog.appendChild(options.content);
                return new Promise((resolve) => asked.push({ dialog, options, answer: resolve }));
            },
        },
    };
    const context = createContext({
        window,
        document: { createElement: (tag) => new Node_(tag) },
        Node_,
        console,
    });
    runInContext(`
        var state = {answers: {}, copyPlaces: {}};
        function el(tag, className, text) { return new Node_(tag, className, text); }
        function layerOf(sample, question) {
            const id = String(question.scope || "").replace(/^layer:/, "");
            return (sample?.layers || []).find((layer) => layer.id === id) || null;
        }
        function questionsFor(sample, scope) {
            return (sample.questions || []).filter((question) => question.scope === scope);
        }
        function basename(path) { return String(path || "").split(/[\\\\/]/).pop(); }
        function render() {}
        function websitePyramids() { return globalThis.__pyramids; }
        ${["formatBytes", "formatCount", "fileChip", "copyBytes", "compressesPyramid", "pyramidWords",
           "compressSwitch", "askPyramid", "askPyramidHome", "renderConfirmNote"].map(lift).join("\n")}
        globalThis.api = {askPyramid, renderConfirmNote, state};
    `, context);
    context.__pyramids = pyramids;
    return { api: context.api, asked, toasts };
}

const GB = 1024 ** 3;

function sampleWith(pyramid) {
    const question = {
        id: "pyramidize:/data/slide.tif", scope: "layer:slide", kind: "confirm", default: "yes",
        options: [{ value: "yes", label: "Pyramidize and import" }, { value: "no", label: "Import as is" }],
    };
    const sample = {
        layers: [{ id: "slide", label: "slide.tif", src: "/data/slide.tif", render: { pyramid } }],
        questions: [question],
    };
    return { sample, question };
}

const FLAT = {
    width: 40000, height: 30000, channels: 30, location: "beside the original",
    output_bytes_estimate: 96 * GB, compressed_bytes_estimate: 15.4 * GB,
    output_names: { compressed: "slide.pyramid.approx", exact: "slide.pyramid.ome.tiff" },
};

const switchOf = (dialog) => dialog.all().find((node) => node.tagName === "INPUT") || null;
const titleOf = (dialog) => dialog.querySelector(".plx-dialog-title").textContent;
const bodyOf = (dialog) => dialog.querySelector(".plx-confirm-body").textContent;
const chipOf = (dialog) => dialog.querySelector(".plx-confirm-chip-meta")?.textContent || "";

function flip(input) {
    input.checked = !input.checked;
    input.fire("change");
}

// -- where the copy can be compressed: the switch, live -----------------------

{
    const { api, asked } = world();
    const { sample, question } = sampleWith({ ...FLAT, compressible: true, compress_default: true });
    const answered = api.askPyramid(question, sample);
    const { dialog, answer } = asked[0];
    const input = switchOf(dialog);
    check("a compressible image's question has the switch", input !== null);
    check("it is a switch, labelled Compress the copy",
          input?.attrs.role === "switch" && input.parent.text() === "Compress the copy");
    check("it starts on, as the setting is", input?.checked === true);
    check("on: the compressed question's title", titleOf(dialog) === "Build a compressed pyramid?", titleOf(dialog));
    // No percentage: the chip says the size, and the switch shows the other.
    check("on: the body says compressed and within its noise",
          /a compressed copy, every pixel within its noise, beside the original, and opens that copy\./
              .test(bodyOf(dialog)) && !/%/.test(bodyOf(dialog)), bodyOf(dialog));
    check("on: the body says the original is not changed", bodyOf(dialog).endsWith("Your original is not changed."));
    check("on: the chip says the compressed copy's size", chipOf(dialog).includes("copy ≈ 15 GB"), chipOf(dialog));

    flip(input);
    check("one dialog the whole time: flipping reopens nothing", asked.length === 1);
    check("off: the title is the exact question's", titleOf(dialog) === "Build a pyramid first?", titleOf(dialog));
    check("off: no compression clause",
          !/compressed|noise/.test(bodyOf(dialog)) && bodyOf(dialog).includes("Plexora saves a copy beside the original"),
          bodyOf(dialog));
    check("off: still says the original is not changed", bodyOf(dialog).endsWith("Your original is not changed."));
    check("off: the chip says the exact copy's size", chipOf(dialog).includes("copy ≈ 96 GB"), chipOf(dialog));
    check("off: one chip, not a second", dialog.all().filter((node) => node.className === "plx-confirm-chip").length === 1);

    flip(input);
    check("on again: all three back at once",
          titleOf(dialog) === "Build a compressed pyramid?" && bodyOf(dialog).includes("a compressed copy")
          && chipOf(dialog).includes("15 GB"));
    flip(input);
    answer("yes");
    check("Pyramidize with the switch off answers exact", (await answered) === "exact");
}

{
    const { api, asked } = world();
    const { sample, question } = sampleWith({ ...FLAT, compressible: true, compress_default: true });
    const answered = api.askPyramid(question, sample);
    asked[0].answer("yes");
    check("Pyramidize with the switch on answers compressed", (await answered) === "compressed");
}

{
    const { api, asked } = world();
    const { sample, question } = sampleWith({ ...FLAT, compressible: true, compress_default: true });
    const answered = api.askPyramid(question, sample);
    asked[0].answer("no");
    check("Import as is answers no", (await answered) === "no");
    const closed = api.askPyramid(question, sample);
    asked[1].answer(null);
    check("the X answers null", (await closed) === null);
}

{
    const { api, asked } = world();
    const { sample, question } = sampleWith({ ...FLAT, compressible: true, compress_default: false });
    api.askPyramid(question, sample);
    const { dialog } = asked[0];
    check("with the setting off it starts off, on the exact question",
          switchOf(dialog)?.checked === false && titleOf(dialog) === "Build a pyramid first?");
}

{
    const { api, asked } = world();
    const { sample, question } = sampleWith({ ...FLAT, compressible: true, compress_default: true });
    api.state.answers[question.id] = "exact";
    api.askPyramid(question, sample);
    check("Change… starts where the last answer left it", switchOf(asked[0].dialog)?.checked === false);
}

// -- where it cannot: today's question, no switch -----------------------------

{
    const { api, asked } = world();
    const { sample, question } = sampleWith({ ...FLAT, compressible: false });
    const answered = api.askPyramid(question, sample);
    const { dialog, answer } = asked[0];
    check("no switch where the copy cannot be compressed", switchOf(dialog) === null);
    check("no switch: the exact question, the exact size",
          titleOf(dialog) === "Build a pyramid first?" && chipOf(dialog).includes("96 GB"));
    answer("yes");
    check("no switch: Pyramidize answers yes, as before", (await answered) === "yes");
}

// -- the row's note -------------------------------------------------------------

{
    const { api } = world();
    const { sample, question } = sampleWith({ ...FLAT, compressible: true, compress_default: true });
    check("before Import the note offers a compressed copy, at its size",
          api.renderConfirmNote(question, sample).text()
              .includes("offer to build a compressed pyramidized copy (about 15 GB)"));
    api.state.answers[question.id] = "compressed";
    check("answered compressed: the note names the compressed copy",
          api.renderConfirmNote(question, sample).text().includes("Builds slide.pyramid.approx (compressed) first."));
    api.state.answers[question.id] = "exact";
    check("answered exact: the note names the exact copy",
          api.renderConfirmNote(question, sample).text().includes("Builds slide.pyramid.ome.tiff first."));
}

// -- the website: the page builds exact copies; no switch ------------------------

{
    const pyramids = { canWriteFolders: () => true, chooseFolder: async () => null, forgetFolder: () => {} };
    const { api, asked } = world({ pyramids, store: { current: () => null } });
    const { sample, question } = sampleWith({ ...FLAT, compressible: false, location: "in this browser's storage for this site" });
    const answered = api.askPyramid(question, sample);
    const { dialog, answer } = asked[0];
    check("the website's folder question has no switch", switchOf(dialog) === null);
    check("and says the original is not changed", bodyOf(dialog).endsWith("Your original is not changed."), bodyOf(dialog));
    answer(null);
    check("closed, it keeps the copy in this browser", (await answered) === "yes");
}

console.log(failures ? `${failures} failed` : "all passed");
process.exit(failures ? 1 : 0);
