// Whole-slide images on the website (plan L2): each slide picked as a user
// picks it -- the home card's Browse, the file, and for an MRXS the folder
// beside it when the page asks -- imported by Plexora's own import, drawn by
// the viewer, and its tiles fetched two ways: from the browser node (the page
// reads the slide with openslide-js) and from Python (which reads the same
// slide through the page). Prints, per slide, the import, the node's spec and
// every tile's bytes for tests/test_browser_slides.py to hold to OpenSlide.
//
//   node tests/browser/slides.mjs --slides name=PATH[,name=PATH] [--shots DIR] [--tiles N]
//
// PATH is the slide's file (an MRXS's .mrxs: its folder of the same name is
// picked from beside it). The File System Access pickers are native dialogs
// no driver can answer, so the page's other path (<input type=file>, and
// <input webkitdirectory> for the folder) is taken by hiding them.
import path from "node:path";
import { REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const slides = opt("--slides", "").split(",").filter(Boolean).map((s) => s.split("="));
const shots = opt("--shots", null);
const perLevel = Number(opt("--tiles", "3"));
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));
if (!slides.length) throw new Error("--slides name=PATH,...");

const server = await startServer(["--site", site]);
const browser = await launch({ headed: args.includes("--headed") });
const out = { slides: {} };

async function home(page) {
    await page.goto(`http://127.0.0.1:${server.port}/`);
    const frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    await frame.waitForFunction(() => {
        const keep = [...document.querySelectorAll(".plx-confirm .plx-button")]
            .find((button) => button.textContent.trim() === "Keep in browser");
        if (!keep) return false;
        keep.click();
        return true;
    }, null, { timeout: 5000, polling: 200 }).catch(() => null);
    await page.evaluate(() => { window.showOpenFilePicker = undefined; window.showDirectoryPicker = undefined; });
    return frame;
}

async function viewerUp(frame) {
    return frame.waitForFunction(() => {
        const viewer = window.__plexora?.seaDragonViewer?.viewer;
        if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
        for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
            if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
        }
        return true;
    }, null, { timeout: 240000, polling: 250 }).then(() => true, () => false);
}

try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text().slice(0, 300)); });
    page.on("response", (response) => {
        if (response.status() >= 400) errors.push(`${response.status()} ${response.url().replace(/^https?:\/\/[^/]+/, "")}`);
    });
    for (const [name, file] of slides) {
        const row = { file };
        out.slides[name] = row;
        const frame = await home(page);
        const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
        await frame.click("#quick_view_go");
        const picked = Date.now();
        await (await chooser).setFiles(file);
        if (/\.mrxs$/i.test(file)) {
            // The page asks for the folder beside the .mrxs; its button is the
            // gesture the folder picker needs.
            const folderChooser = page.waitForEvent("filechooser", { timeout: 30000 });
            await frame.waitForSelector(".plx-confirm .plx-button-primary", { timeout: 30000 });
            row.asked = await frame.evaluate(() => document.querySelector(".plx-confirm")?.innerText.trim());
            await frame.click(".plx-confirm .plx-button-primary");
            await (await folderChooser).setFiles(file.replace(/\.mrxs$/i, ""));
        }
        let moved = false;
        const until = Date.now() + 300000;
        while (!moved && Date.now() < until) {
            moved = await frame.evaluate(() => location.pathname !== "/" && window.PlexoraShellFramed
                                         && !document.getElementById("plexora-boot")).catch(() => false);
            if (moved) break;
            await frame.evaluate(() => {
                const confirm = document.querySelector(".plx-confirm .plx-button-primary");
                if (confirm && confirm.offsetParent !== null) confirm.click();
                const go = document.querySelector('[data-role="go"]');
                if (go && !go.disabled && go.offsetParent !== null && !go.dataset.pressedByTest) {
                    go.dataset.pressedByTest = "1";
                    go.click();
                }
            }).catch(() => null);
            await page.waitForTimeout(500);
        }
        if (!moved) {
            row.stuck = await frame.evaluate(() => document.body.innerText.slice(0, 600)).catch(() => null);
            row.errors = errors.splice(0);
            if (shots) await page.screenshot({ path: path.join(shots, `slides-${name}-stuck.png`) });
            continue;
        }
        row.importMs = Date.now() - picked;
        row.viewerUp = await viewerUp(frame);
        row.viewerMs = Date.now() - picked;
        if (shots) await page.screenshot({ path: path.join(shots, `slides-${name}.png`) });
        // The project's name and its tile grid, from the viewer's own source.
        const source = await frame.evaluate(() => {
            const item = window.__plexora.seaDragonViewer.viewer.world.getItemAt(0);
            return { url: item.source.getTileUrl(item.source.maxLevel, 0, 0) };
        });
        const project = decodeURIComponent(source.url.split("/generated/data/")[1].split("/")[0]);
        row.project = project;
        row.spec = await page.evaluate((p) => window.PlexoraPython.nodeSpec(p), project);
        // The picture the user sees, by pixels: how much of the canvas is not
        // the page's empty background.
        row.canvas = await frame.evaluate(() => {
            const canvas = document.querySelector("#openseadragon canvas, .openseadragon-canvas canvas");
            if (!canvas) return null;
            const copy = document.createElement("canvas");
            copy.width = 160; copy.height = 100;
            const ctx = copy.getContext("2d");
            ctx.drawImage(canvas, 0, 0, copy.width, copy.height);
            const data = ctx.getImageData(0, 0, copy.width, copy.height).data;
            let tinted = 0;
            for (let i = 0; i < data.length; i += 4) {
                const max = Math.max(data[i], data[i + 1], data[i + 2]);
                const min = Math.min(data[i], data[i + 1], data[i + 2]);
                if (max - min > 24) tinted += 1;
            }
            return { tintedFraction: tinted / (data.length / 4) };
        });
        // Tiles: on every level the node serves, the corner, the middle and the
        // last, from the browser node and from Python.
        const levels = row.spec.served && row.spec.kind === "slide" ? row.spec.slide.levels : [];
        const [tw, th] = row.spec.tile || [1024, 1024];
        const wanted = [];
        levels.forEach((level, index) => {
            const nx = Math.ceil(level.width / tw);
            const ny = Math.ceil(level.height / th);
            const picks = [[0, 0], [Math.floor(nx / 2), Math.floor(ny / 2)], [nx - 1, ny - 1]].slice(0, perLevel);
            for (const [tx, ty] of picks) wanted.push({ level: index, tx, ty });
        });
        row.tiles = await page.evaluate(async ({ project: p, wanted: list }) => {
            const b64 = (buffer) => {
                const bytes = new Uint8Array(buffer);
                let text = "";
                for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
                return btoa(text);
            };
            const found = [];
            for (const { level, tx, ty } of list) {
                const url = `/generated/data/${encodeURIComponent(p)}/rgb/${level}/${tx}_${ty}.png?q=hd`;
                const t0 = performance.now();
                const node = await window.PlexoraNode.answer(new Request(new URL(url, location.origin)));
                const nodeMs = performance.now() - t0;
                const t1 = performance.now();
                const python = await window.PlexoraPython.fetch(url);
                const pythonMs = performance.now() - t1;
                found.push({ level, tx, ty, nodeMs, pythonMs,
                             node: node ? b64(await node.arrayBuffer()) : null,
                             python: python.ok ? b64(await python.arrayBuffer()) : null,
                             pythonStatus: python.status,
                             pythonType: python.headers.get("Content-Type") });
            }
            return found;
        }, { project, wanted });
        row.errors = errors.splice(0);
    }
} catch (error) {
    out.error = String(error && error.stack || error);
    process.exitCode = 1;
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out));
