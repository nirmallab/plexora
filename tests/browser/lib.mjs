// What the browser tests share: the dev server (tools/browser_build/serve.py)
// and a Chrome to open its pages in. Chrome is the system's where there is
// one (`PLEXORA_BENCH_CHROME` names another), else Playwright's own Chromium
// (CI: `playwright-core install chromium`); playwright-core comes from
// tools/poc_browser_node (`npm ci` there). `PLEXORA_PYTHON` runs serve.py.
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
    const where = path.join(REPO, "tools", "poc_browser_node", "node_modules", "playwright-core", "index.mjs");
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

export async function launch({ headed = false, args = [] } = {}) {
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
