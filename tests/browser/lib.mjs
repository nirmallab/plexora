// What the browser tests share: the dev server (tools/browser_build/serve.py)
// and a Chrome to open its pages in. Chrome is the system's where there is
// one (`PLEXORA_BENCH_CHROME` names another), else Playwright's own Chromium
// (CI: `playwright-core install chromium`); playwright-core comes from
// tools/poc_browser_node (`npm ci` there), or the node_modules folder
// `PLEXORA_PLAYWRIGHT_MODULES` names (a worktree whose own is not usable).
// `PLEXORA_PYTHON` runs serve.py.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO = path.resolve(HERE, "..", "..");
export const CHROME = process.env.PLEXORA_BENCH_CHROME
    || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
export const PYTHON = process.env.PLEXORA_PYTHON || "C:/Users/aj/.conda/envs/plexora/python.exe";

export async function playwright() {
    // `PLEXORA_PLAYWRIGHT_CORE` names a playwright-core package folder,
    // `PLEXORA_PLAYWRIGHT_MODULES` a node_modules folder holding one.
    const modules = process.env.PLEXORA_PLAYWRIGHT_MODULES
        || path.join(REPO, "tools", "poc_browser_node", "node_modules");
    const where = process.env.PLEXORA_PLAYWRIGHT_CORE
        ? path.join(process.env.PLEXORA_PLAYWRIGHT_CORE, "index.mjs")
        : path.join(modules, "playwright-core", "index.mjs");
    return import(pathToFileURL(where).href);
}

export function startServer(extra = []) {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON, [path.join(REPO, "tools", "browser_build", "serve.py"), ...extra],
                            { cwd: REPO, stdio: ["ignore", "pipe", "inherit"] });
        let buffer = "";
        child.stdout.on("data", (chunk) => {
            buffer += chunk;
            const line = buffer.split("\n")[0];
            if (buffer.includes("\n")) {
                try { resolve({ port: JSON.parse(line).port, stop: () => child.kill() }); }
                catch (error) { reject(error); }
            }
        });
        child.on("exit", (code) => reject(new Error(`serve.py exited ${code}`)));
    });
}

//: The software GPU a CI runner draws WebGL with (it has no GPU): headless
//: Chromium falls back to SwiftShader only when told it may.
export const SWIFTSHADER = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"];

//: Which engine the drivers open: `--browser chromium|firefox|webkit` on the
//: driver's command line, else `PLEXORA_BROWSER`, else Chromium (the system
//: Chrome). Firefox and WebKit are Playwright's own builds
//: (`playwright-core install firefox webkit`); WebKit is Safari's engine, built
//: for Windows here (it has no origin private file system on Windows).
export const BROWSER = (() => {
    const argv = process.argv;
    const named = argv.includes("--browser") ? argv[argv.indexOf("--browser") + 1]
        : process.env.PLEXORA_BROWSER || "chromium";
    const engine = String(named).toLowerCase().replace(/^chrome$/, "chromium").replace(/^safari$/, "webkit");
    if (!["chromium", "firefox", "webkit"].includes(engine)) throw new Error(`--browser ${named}: chromium, firefox or webkit`);
    return engine;
})();

export const NO_JSPI = process.argv.includes("--no-jspi") || process.env.PLEXORA_NO_JSPI === "1";

export async function launch({ headed = false, args = [], browser = BROWSER } = {}) {
    if (browser !== "chromium") return launchOther(browser, { headed });
    const { chromium } = await playwright();
    const profile = mkdtempSync(path.join(tmpdir(), "pxweb-chrome-"));
    // The system Chrome where there is one (this machine); elsewhere (CI)
    // the Chromium `playwright-core install chromium` put in its cache.
    const executablePath = (process.env.PLEXORA_BENCH_CHROME || existsSync(CHROME)) ? CHROME : undefined;
    const context = await chromium.launchPersistentContext(profile, {
        executablePath, headless: !headed,
        args: ["--no-first-run", "--no-default-browser-check", "--disable-background-timer-throttling",
               "--disable-renderer-backgrounding", ...args],
    });
    return {
        context,
        profile,
        close: async () => {
            await context.close();
            try { rmSync(profile, { recursive: true, force: true }); } catch { /* Windows may hold it */ }
        },
    };
}

//: Whether pages on `origin` can ask for a folder to write (File System
//: Access, Chromium only). Drivers stand a folder in for the picker only where
//: there is one: elsewhere the site must take its other path.
export async function canWriteFolders(context, origin) {
    const page = await context.newPage();
    try {
        await page.goto(`${origin}/client/shell.json`);
        return await page.evaluate(() => typeof window.showDirectoryPicker === "function");
    } finally {
        await page.close();
    }
}

//: Firefox or WebKit, Playwright's builds, in a fresh profile of their own.
async function launchOther(engine, { headed = false } = {}) {
    const pw = await playwright();
    const profile = mkdtempSync(path.join(tmpdir(), `pxweb-${engine}-`));
    const options = { headless: !headed };
    if (engine === "firefox") {
        // Timers in a background page run as Chrome's flags above ask of it.
        options.firefoxUserPrefs = { "dom.min_background_timeout_value": 4,
                                     "dom.timeout.enable_budget_timer_throttling": false };
        // `--no-jspi` (or PLEXORA_NO_JSPI=1): a Firefox without JavaScript
        // Promise Integration, as releases before it shipped are -- the
        // website's other path (workers/pythonWorker.js `syncAsk`).
        if (NO_JSPI) options.firefoxUserPrefs["javascript.options.wasm_js_promise_integration"] = false;
    }
    const context = await pw[engine].launchPersistentContext(profile, options);
    return {
        context,
        engine,
        close: async () => {
            await context.close();
            try { rmSync(profile, { recursive: true, force: true }); } catch { /* Windows may hold it */ }
        },
    };
}

//: The app's frame on a page of the site (services/shell.js shows the app in
//: one), once its first page has been written.
export async function appFrame(page, { timeout = 120000 } = {}) {
    const element = await page.waitForSelector("#plexora-app", { timeout });
    const frame = await element.contentFrame();
    await frame.waitForFunction(() => window.PlexoraShellFramed && document.body
                                && !document.getElementById("plexora-boot"), null, { timeout });
    return frame;
}

//: The website's storage choice made before a driver's first page, so the
//: first-use onboarding (views/webOnboarding.js) never shows: `record` is
//: written to IndexedDB plexora-store/store/data from a throwaway page of the
//: site, and awaited -- not an init script, which would race the boot's own
//: read of it (services/browserStore.js `prepare`). The default keeps the
//: projects in the browser, as "Use browser storage" does.
export async function seedBrowserStore(context, origin, record = { kind: "browser", chosen: true, onboarded: true }) {
    const page = await context.newPage();
    try {
        await page.goto(`${origin}/client/shell.json`);
        await page.evaluate((value) => new Promise((resolve, reject) => {
            const request = indexedDB.open("plexora-store", 1);
            request.onupgradeneeded = () => request.result.createObjectStore("store");
            request.onerror = () => reject(request.error);
            request.onsuccess = () => {
                const db = request.result;
                const tx = db.transaction("store", "readwrite");
                tx.objectStore("store").put(value, "data");
                tx.oncomplete = () => { db.close(); resolve(true); };
                tx.onerror = () => reject(tx.error);
            };
        }), record);
    } finally {
        await page.close();
    }
}

//: The onboarding answered in the app's frame, once it is up:
//:   "browser"    new, Use browser storage
//:   "folder"     new, Use a folder on this computer, Choose folder (the
//:                host's showDirectoryPicker must be stood in for), and Use
//:                this folder where it already holds something
//:   "returning"  used before, Locate my Plexora folder, then Continue (or Set
//:                up this folder where it is not one)
//:   "reconnect"  Reconnect folder, on the Reconnect screen
//:   "cancel"     the X
//: Resolves {screens: [screen names seen, in order], title: the last one's}.
export async function driveOnboarding(frame, choice = "browser", { timeout = 60000 } = {}) {
    const screens = [];
    const live = (selector) => `dialog.plx-onb .plx-onb-screen:not(.is-leaving) ${selector}`;
    const screen = () => frame.evaluate(() => document.querySelector("dialog.plx-onb")?.dataset.screen || null);
    const title = () => frame.evaluate(() => document.querySelector(
        "dialog.plx-onb .plx-onb-screen:not(.is-leaving) .plx-onb-title")?.textContent || null);
    const press = async (selector) => {
        const node = await frame.waitForSelector(live(selector), { state: "visible", timeout });
        screens.push(await screen());
        await node.click();
    };
    //: The first of `selectors` on screen, once one is.
    const first = async (selectors) => {
        const handle = await frame.waitForFunction((list) => list.find((one) => document.querySelector(
            `dialog.plx-onb .plx-onb-screen:not(.is-leaving) ${one}`)) || null, selectors, { timeout, polling: 100 });
        return handle.jsonValue();
    };
    await frame.waitForSelector("dialog.plx-onb[open]", { timeout });
    if (choice === "cancel") {
        screens.push(await screen());
        const shown = await title();
        await frame.click("dialog.plx-onb .plx-onb-close");
        await frame.waitForSelector("dialog.plx-onb", { state: "detached", timeout });
        return { screens, title: shown };
    }
    if (choice === "browser") {
        await press('[data-choice="new"]');
        await press('[data-choice="browser"]');
    } else if (choice === "folder") {
        await press('[data-choice="new"]');
        await press('[data-choice="folder"]');
        await press('[data-role="choose"]');
        if (await first(['[data-role="use"]', '[data-role="continue"]']) === '[data-role="use"]') {
            await press('[data-role="use"]');
        }
    } else if (choice === "returning") {
        await press('[data-choice="returning"]');
        await press('[data-role="locate"]');
        const next = await first(['[data-role="continue"]', '[data-role="init"]']);
        if (await screen() !== "setup") await press(next);
    } else if (choice === "reconnect") {
        await press('[data-role="reconnect"]');
        await frame.waitForSelector("dialog.plx-onb", { state: "detached", timeout });
        return { screens, title: null };
    } else {
        throw new Error(`driveOnboarding: no choice "${choice}"`);
    }
    const go = await frame.waitForSelector('dialog.plx-onb[data-phase="done"] [data-role="continue"]', { timeout });
    screens.push(await screen());
    const shown = await title();
    await go.click();
    await frame.waitForSelector("dialog.plx-onb", { state: "detached", timeout });
    return { screens, title: shown };
}
