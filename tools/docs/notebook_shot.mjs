#!/usr/bin/env node
// Captures notebook/cell and home/explore-notebook: a JupyterLab notebook
// whose one cell runs `plexora.view("exemplar-001", height=600)`.
//
//   node tools/docs/notebook_shot.mjs http://127.0.0.1:8899/lab/tree/analysis.ipynb
//
// Start JupyterLab yourself first, with no token, on a notebook holding only
// that cell, and a kernel that imports this checkout's plexora with the same
// unlicensed copy of the demo projects screenshots.mjs uses (PLEXORA_DATA_PATH
// and an empty BIOCOGNIA_DIR in the kernelspec's env). CONTRIBUTING_DOCS.md
// has the recipe. IPython's warning about an <iframe> in HTML output is
// hidden from the shot; the cell output is otherwise as a reader sees it.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Page } from "./screenshots.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(HERE, "..", "..", "website", "public", "media");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const url = process.argv[2];
if (!url) throw new Error("usage: notebook_shot.mjs <JupyterLab URL of the notebook>");

const page = new Page();
await page.launch(false);
try {
    await page.goto(url);
    await page.waitFor("document.querySelector('.jp-Notebook .jp-CodeCell')", 60000);
    await sleep(4000);
    // The news prompt, stderr and a folded file browser.
    await page.eval(`[...document.querySelectorAll('.jp-Notification-Toast button, .Toastify button')]
        .find((b) => b.textContent.trim() === 'No')?.click()`).catch(() => {});
    await page.eval(`(() => {
        const s = document.createElement('style');
        s.textContent = '.jp-OutputArea-output[data-mime-type="application/vnd.jupyter.stderr"], .Toastify { display: none !important; }';
        document.head.append(s);
    })()`);
    if (await page.eval("Boolean(document.querySelector('.jp-FileBrowser')?.offsetParent)")) await page.mouse(16, 54);
    await sleep(800);
    // Run the cell: Shift+Enter in its editor.
    await page.click(".jp-CodeCell .cm-content");
    await sleep(500);
    for (const type of ["keyDown", "keyUp"]) {
        await page.send("Input.dispatchKeyEvent", { type, key: "Enter", code: "Enter", modifiers: 8, windowsVirtualKeyCode: 13 });
    }
    await page.waitFor("document.querySelector('.jp-OutputArea iframe')", 120000);
    await sleep(15000);
    await page.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 5, y: 790 });
    await sleep(1500);
    await page.shot(path.join(OUT, "notebook", "cell.webp"));
    // The home tile: the code cell and the viewer under it, at 16:10.
    await page.shot(path.join(OUT, "home", "explore-notebook.webp"),
                    { x: 96, y: 100, width: 1120, height: 700, scale: 1280 / 1120 });
    console.log("captured notebook/cell and home/explore-notebook");
} finally {
    page.close();
}
process.exit(0);
