// The website's first-use onboarding (views/webOnboarding.js), driven in a
// real browser against the static build and its Python.
//
// Nothing is asked on load; the first image picked brings up the dialog, and
// once it is answered the image opens without a second file dialog. The
// browser's folder dialog is a native window no driver can answer, so the
// host page's `showDirectoryPicker` hands back folders in the profile's own
// storage (OPFS test-folders/<name>), which is what a File System Access
// handle is either way; the image comes through <input type=file>.
//
//   node tests/browser/onboarding.mjs --site DIR --file IMAGE [--case NAME ...] [--shots DIR] [--headed]
//
// Cases (all by default), each in a fresh profile:
//   quiet        the home page loads, Python starts, and no dialog shows
//   browser      new, browser storage; the image opens; reload: no dialog
//   folder       new, a folder named Plexora; the image opens; the folder is
//                Plexora/{workspace.json, projects, pyramids}, never
//                Plexora/Plexora; reload: no dialog, Recent lists it
//   imaging      the same into a folder named Imaging: Imaging/{...}, no
//                Imaging/Plexora, and the desktop note
//   found        used before: a workspace already there is found and adopted
//   unknown      used before: a folder that is not one is said to be not one
//   legacy       used before: Plexora/Plexora/projects (an old version's) is
//                found and adopted where it is
//   cancel       the X: the pick is dropped, nothing mounted or remembered
//   denied       the browser refuses to let the page change the folder
//   nopicker     no folder dialog (Firefox, Safari): the folder option is
//                replaced by why, and browser storage is chosen
//   reconnect    a folder chosen on a visit before, not granted on this one:
//                the home page's Reconnect card, and a click opens it again
//
// `--inject` adds the onboarding to every page through toast.js, and the home
// page's Reconnect card (its markup from the build's client/home.html) ahead
// of quickViewLanding.js, for a Python bundle (`static_build --py`) older than
// this checkout's templates, whose pages have neither yet. Run node with
// PW_EXPERIMENTAL_SERVICE_WORKER_NETWORK_EVENTS=1 then: the site's worker
// fetches the files itself.
import { readFileSync } from "node:fs";
import path from "node:path";
import { REPO, appFrame, driveOnboarding, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const site = opt("--site", null);
const file = opt("--file", null);
const shots = opt("--shots", null);
if (!site || !file) throw new Error("--site DIR (static_build's output) and --file IMAGE are required");
const ALL = ["quiet", "browser", "folder", "imaging", "found", "unknown", "legacy", "cancel", "denied", "nopicker", "reconnect"];
const chosen = args.filter((arg, i) => args[i - 1] === "--case");
const cases = chosen.length ? chosen : ALL;

//: Before the page's own scripts, in every frame: the folder dialog stood in
//: for (a folder in OPFS named by `localStorage["test-pick"]`), the file
//: dialog left to <input type=file>, and, where `localStorage["test-lapse"]`
//: says so, folders the browser asks about again until a click grants them.
const STUB = () => {
    const want = (key, fallback) => { try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; } };
    window.showOpenFilePicker = undefined;
    if (want("test-nopicker", "") === "1") {
        delete window.showDirectoryPicker;
        window.showDirectoryPicker = undefined;
    } else {
        window.__picked = [];
        window.showDirectoryPicker = async (options = {}) => {
            window.__picked.push({ id: options.id, mode: options.mode, startIn: typeof options.startIn === "string"
                ? options.startIn : options.startIn ? "handle" : null });
            const root = await navigator.storage.getDirectory();
            const top = await root.getDirectoryHandle("test-folders", { create: true });
            const handle = await top.getDirectoryHandle(want("test-pick", "Plexora"), { create: true });
            if (want("test-deny", "") === "1") {
                handle.queryPermission = async () => "prompt";
                handle.requestPermission = async () => "denied";
            }
            return handle;
        };
    }
    if (want("test-lapse", "") === "1" && window.FileSystemHandle) {
        let granted = false;
        const proto = window.FileSystemHandle.prototype;
        const query = proto.queryPermission;
        proto.queryPermission = function (options) { return granted ? query.call(this, options) : Promise.resolve("prompt"); };
        proto.requestPermission = function () { granted = true; return Promise.resolve("granted"); };
    }
};

//: OPFS test-folders/, as "path size" lines.
const LIST = async () => {
    const walk = async (dir, prefix = "") => {
        const out = [];
        for await (const [name, handle] of dir.entries()) {
            if (handle.kind === "directory") {
                out.push(`${prefix}${name}/`);
                out.push(...await walk(handle, `${prefix}${name}/`));
            } else {
                out.push(`${prefix}${name}`);
            }
        }
        return out;
    };
    const root = await navigator.storage.getDirectory();
    return walk(await root.getDirectoryHandle("test-folders", { create: true }));
};

//: Files written into OPFS test-folders/ before the page asks for them.
const SEED = async (files) => {
    const root = await navigator.storage.getDirectory();
    let top = await root.getDirectoryHandle("test-folders", { create: true });
    for (const [where, text] of Object.entries(files)) {
        const parts = where.split("/");
        const name = parts.pop();
        let dir = top;
        for (const part of parts) dir = await dir.getDirectoryHandle(part, { create: true });
        const writable = await (await dir.getFileHandle(name, { create: true })).createWritable();
        await writable.write(text);
        await writable.close();
    }
    top = null;
    return true;
};

const server = await startServer(["--site", site]);
const origin = `http://127.0.0.1:${server.port}`;
const results = {};
const failures = [];

async function run(name, body) {
    const browser = await launch({ headed: args.includes("--headed") });
    const out = { checks: {}, errors: [] };
    const expect = (label, ok, detail) => {
        out.checks[label] = Boolean(ok);
        if (!ok) failures.push(`${name}: ${label}${detail === undefined ? "" : ` (${JSON.stringify(detail)})`}`);
    };
    try {
        await browser.context.addInitScript(STUB);
        if (args.includes("--inject")) {
            const onboarding = readFileSync(path.join(REPO, "plexora/client/src/js/views/webOnboarding.js"), "utf8");
            await browser.context.route(/\/client\/src\/js\/services\/toast\.js/, async (route) => {
                const response = await route.fetch();
                const body = `${await response.text()}\n;if (!window.PlexoraOnboarding) {\n${onboarding}\n}\n`;
                await route.fulfill({ response, body });
            });
            const home = readFileSync(path.join(site, "client", "home.html"), "utf8");
            const at = home.indexOf('<section class="plx-onb-reconnect"');
            const card = at < 0 ? "" : home.slice(at, home.indexOf("</section>", at) + "</section>".length);
            await browser.context.route(/\/client\/src\/js\/views\/quickViewLanding\.js/, async (route) => {
                const response = await route.fetch();
                const shim = `(function () { const landing = document.querySelector(".quick-view-column");
                    if (landing && !document.getElementById("quick_view_reconnect")) {
                        const holder = document.createElement("div"); holder.innerHTML = ${JSON.stringify(card)};
                        const recent = landing.querySelector(".quick-view-recent");
                        landing.insertBefore(holder.firstElementChild, recent || null);
                    } })();\n`;
                await route.fulfill({ response, body: shim + await response.text() });
            });
        }
        const page = await browser.context.newPage();
        await page.setViewportSize({ width: 1280, height: 860 });
        page.on("pageerror", (error) => out.errors.push(`pageerror: ${error}`));
        // The first page of the site, so localStorage and OPFS are this origin's.
        await page.goto(`${origin}/client/shell.json`);
        await body({ page, out, expect });
    } catch (error) {
        failures.push(`${name}: ${error && error.stack ? error.stack.split("\n").slice(0, 3).join(" | ") : error}`);
    } finally {
        results[name] = out;
        await browser.close();
    }
}

async function open(page) {
    await page.goto(`${origin}/?pydebug`);
    const frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    return frame;
}

const started = (page) => page.waitForFunction(() => window.PlexoraSite && window.PlexoraSite.version, null,
                                               { timeout: 180000 });
const dialogUp = (frame) => frame.evaluate(() => Boolean(document.querySelector("dialog.plx-onb[open]")));
const record = (page) => page.evaluate(() => new Promise((resolve) => {
    const request = indexedDB.open("plexora-store", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("store");
    request.onsuccess = () => {
        const get = request.result.transaction("store").objectStore("store").get("data");
        get.onsuccess = () => {
            const value = get.result || null;
            resolve(value && { ...value, home: value.home ? value.home.name : null, locations: (value.locations || []).length });
        };
    };
}));

//: Browse, the image picked in the file dialog: resolves when the dialog has
//: been answered with the file. `onboard` answers the onboarding meanwhile.
async function browse(page, frame, onboard) {
    const choosers = [];
    page.on("filechooser", (chooser) => choosers.push(chooser));
    const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click("#quick_view_go");
    await (await chooser).setFiles(file);
    const answer = onboard ? await onboard() : null;
    return { answer, choosers };
}

const imageOpened = (frame) => frame.waitForFunction(() => location.pathname !== "/" && !document.getElementById("plexora-boot"),
                                                     null, { timeout: 240000, polling: 250 }).then(() => true, () => false);

async function shoot(page, label) {
    if (shots) await page.screenshot({ path: path.join(shots, `onboarding-${label}.png`) });
}

async function firstUse({ page, out, expect }, choice, { pick = "Plexora", expectTree = null } = {}) {
    await page.evaluate((name) => localStorage.setItem("test-pick", name), pick);
    let frame = await open(page);
    expect("no dialog on load", !await dialogUp(frame));
    const { answer, choosers } = await browse(page, frame, async () => {
        await frame.waitForSelector("dialog.plx-onb[open]", { timeout: 30000 });
        await shoot(page, `${choice}-${pick}-welcome`);
        return driveOnboarding(frame, choice, { timeout: 240000 });
    });
    out.onboarding = answer;
    expect("the image opens", await imageOpened(frame));
    expect("one file dialog, not two", choosers.length === 1, choosers.length);
    out.record = await record(page);
    out.store = await page.evaluate(() => window.PlexoraStore.current());
    if (expectTree) {
        out.tree = await page.evaluate(LIST);
        expectTree(out.tree);
    }
    // Reload: the choice is kept, nothing is asked.
    await page.goto(`${origin}/?pydebug`);
    frame = await appFrame(page);
    await started(page);
    await page.waitForTimeout(1500);
    expect("no dialog after a reload", !await dialogUp(frame));
    out.recent = await frame.evaluate(() => document.querySelectorAll(".quick-view-recent-item").length);
    expect("Recent lists the project", out.recent >= 1, out.recent);
    await shoot(page, `${choice}-${pick}-reload`);
    return frame;
}

const checkRoot = (expect, root) => (tree) => {
    expect(`${root}/workspace.json`, tree.includes(`${root}/workspace.json`), tree);
    expect(`${root}/projects/`, tree.includes(`${root}/projects/`), tree);
    expect(`${root}/pyramids/`, tree.includes(`${root}/pyramids/`), tree);
    expect(`no ${root}/Plexora/`, !tree.some((line) => line.startsWith(`${root}/Plexora/`)), tree);
    expect("the project is in the folder", tree.some((line) => line.startsWith(`${root}/projects/config.json`)), tree);
};

for (const name of cases) {
    if (name === "quiet") {
        await run(name, async ({ page, out, expect }) => {
            const frame = await open(page);
            await started(page);
            await page.waitForTimeout(3000);
            expect("no dialog once Python is up", !await dialogUp(frame));
            out.record = await record(page);
            expect("nothing recorded", out.record === null, out.record);
            await shoot(page, "quiet");
        });
    } else if (name === "browser") {
        await run(name, async (ctx) => {
            await firstUse(ctx, "browser");
            ctx.expect("the record is the browser's", ctx.out.record && ctx.out.record.kind === "browser", ctx.out.record);
            ctx.expect("titled Browser storage is ready", ctx.out.onboarding.title === "Browser storage is ready");
        });
    } else if (name === "folder" || name === "imaging") {
        const root = name === "folder" ? "Plexora" : "Imaging";
        await run(name, async (ctx) => {
            await firstUse(ctx, "folder", { pick: root, expectTree: checkRoot(ctx.expect, root) });
            ctx.expect("the record is the folder", ctx.out.record && ctx.out.record.kind === "folder"
                       && ctx.out.record.home === root, ctx.out.record);
            ctx.expect("desktopFinds", ctx.out.store.desktopFinds === (root === "Plexora"), ctx.out.store);
            ctx.expect("titled Your workspace is ready", ctx.out.onboarding.title === "Your workspace is ready");
        });
    } else if (name === "found" || name === "unknown" || name === "legacy") {
        await run(name, async ({ page, out, expect }) => {
            const seeds = {
                found: { "Plexora/workspace.json": JSON.stringify({ schema: 1, id: "f".repeat(32), createdAt: "2026-11-01T00:00:00Z",
                                                                   createdBy: "desktop", name: "Plexora" }),
                         "Plexora/projects/config.json": "{}" },
                unknown: { "Research/notes.txt": "hello" },
                legacy: { "Plexora/Plexora/projects/config.json": "{}" },
            }[name];
            await page.evaluate(SEED, seeds);
            await page.evaluate((pick) => localStorage.setItem("test-pick", pick), name === "unknown" ? "Research" : "Plexora");
            const frame = await open(page);
            const picked = browse(page, frame, async () => {
                await frame.waitForSelector("dialog.plx-onb[open]", { timeout: 30000 });
                await frame.click('dialog.plx-onb [data-choice="returning"]');
                await frame.click('dialog.plx-onb .plx-onb-screen:not(.is-leaving) [data-role="locate"]');
                await frame.waitForFunction((want) => document.querySelector("dialog.plx-onb")?.dataset.screen === want,
                                            name === "unknown" ? "unrecognised" : "found", { timeout: 30000 });
                out.title = await frame.evaluate(() => document.querySelector(
                    "dialog.plx-onb .plx-onb-screen:not(.is-leaving) .plx-onb-title").textContent);
                await shoot(page, `returning-${name}`);
                if (name === "unknown") {
                    await frame.click("dialog.plx-onb .plx-onb-close");
                    return null;
                }
                await frame.click('dialog.plx-onb .plx-onb-screen:not(.is-leaving) [data-role="continue"]');
                await frame.waitForSelector('dialog.plx-onb[data-phase="done"] [data-role="continue"]', { timeout: 240000 });
                await frame.click('dialog.plx-onb[data-phase="done"] [data-role="continue"]');
                return true;
            });
            await picked;
            out.tree = await page.evaluate(LIST);
            if (name === "unknown") {
                expect("said not a Plexora folder", out.title === "“Research” is not a Plexora folder", out.title);
                expect("nothing written into it", out.tree.join(",") === "Research/,Research/notes.txt", out.tree);
                return;
            }
            expect("found", out.title === "Found your workspace", out.title);
            expect("the image opens", await imageOpened(frame));
            out.record = await record(page);
            if (name === "found") {
                expect("the marker kept as it was", out.record.workspaceId === "f".repeat(32), out.record);
            } else {
                expect("adopted where it is", out.record.home === "Plexora" && out.record.label === "Plexora › Plexora", out.record);
                expect("nothing moved", out.tree.includes("Plexora/Plexora/workspace.json")
                       && !out.tree.includes("Plexora/workspace.json"), out.tree);
            }
        });
    } else if (name === "cancel") {
        await run(name, async ({ page, out, expect }) => {
            const frame = await open(page);
            const { answer } = await browse(page, frame, async () => driveOnboarding(frame, "cancel"));
            out.onboarding = answer;
            await page.waitForTimeout(1500);
            expect("still on the home page", await frame.evaluate(() => location.pathname === "/"));
            out.picks = await page.evaluate(async () => (await window.PlexoraPicks.list()).length);
            expect("nothing remembered", out.picks === 0, out.picks);
            out.record = await record(page);
            expect("nothing chosen", out.record === null, out.record);
        });
    } else if (name === "denied") {
        await run(name, async ({ page, out, expect }) => {
            await page.evaluate(() => localStorage.setItem("test-deny", "1"));
            const frame = await open(page);
            await browse(page, frame, async () => {
                await frame.waitForSelector("dialog.plx-onb[open]", { timeout: 30000 });
                await frame.click('dialog.plx-onb [data-choice="new"]');
                await frame.click('dialog.plx-onb .plx-onb-screen:not(.is-leaving) [data-choice="folder"]');
                await frame.click('dialog.plx-onb .plx-onb-screen:not(.is-leaving) [data-role="choose"]');
                await frame.waitForFunction(() => document.querySelector("dialog.plx-onb")?.dataset.screen === "denied",
                                            null, { timeout: 30000 });
                await shoot(page, "denied");
                await frame.click("dialog.plx-onb .plx-onb-close");
            });
            out.tree = await page.evaluate(LIST);
            expect("nothing written", !out.tree.some((line) => line.includes("workspace.json")), out.tree);
        });
    } else if (name === "nopicker") {
        await run(name, async ({ page, out, expect }) => {
            await page.evaluate(() => localStorage.setItem("test-nopicker", "1"));
            const frame = await open(page);
            const { answer } = await browse(page, frame, async () => {
                await frame.waitForSelector("dialog.plx-onb[open]", { timeout: 30000 });
                await frame.click('dialog.plx-onb [data-choice="new"]');
                await frame.waitForSelector("dialog.plx-onb .plx-onb-hint.is-warn", { timeout: 10000 });
                out.folderOption = await frame.evaluate(() => Boolean(document.querySelector('dialog.plx-onb [data-choice="folder"]')));
                await shoot(page, "nopicker");
                await frame.click('dialog.plx-onb .plx-onb-screen:not(.is-leaving) [data-choice="browser"]');
                await frame.waitForSelector('dialog.plx-onb[data-phase="done"] [data-role="continue"]', { timeout: 60000 });
                await frame.click('dialog.plx-onb[data-phase="done"] [data-role="continue"]');
                return true;
            });
            out.onboarding = answer;
            expect("no folder option", out.folderOption === false);
            expect("the image opens", await imageOpened(frame));
        });
    } else if (name === "reconnect") {
        await run(name, async ({ page, out, expect }) => {
            await firstUse({ page, out, expect }, "folder", { pick: "Plexora" });
            // The next visit: the browser asks again for the folder.
            await page.evaluate(() => localStorage.setItem("test-lapse", "1"));
            await page.goto(`${origin}/?pydebug`);
            let frame = await appFrame(page);
            await started(page);
            out.lapsed = await page.evaluate(() => window.PlexoraStore.current());
            expect("left closed, not asked", out.lapsed.needsPermission === true && out.lapsed.kind === "browser", out.lapsed);
            const card = await frame.waitForSelector("#quick_view_reconnect:not([hidden])", { timeout: 30000 })
                .then(() => true, () => false);
            expect("the Reconnect card shows", card);
            expect("no dialog on load", !await dialogUp(frame));
            await shoot(page, "reconnect-card");
            if (!card) return;
            await frame.click('#quick_view_reconnect [data-role="reconnect"]');
            await page.waitForFunction(() => window.PlexoraStore.current().kind === "folder", null, { timeout: 60000 });
            frame = await appFrame(page);
            await page.waitForTimeout(2000);
            out.recent = await frame.evaluate(() => document.querySelectorAll(".quick-view-recent-item").length);
            expect("Recent lists the folder's project", out.recent >= 1, out.recent);
        });
    }
}

server.stop();
console.log(JSON.stringify({ cases, results, failures }, null, 1));
process.exit(failures.length ? 1 : 0);
