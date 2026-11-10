// The same image opens with the same channel contrast on the website and on
// the desktop, and the viewer's automatic contrast lands on both: its first
// pass (the statistics' hint) and its second (the mixture fit), on every
// channel, after a viewer was left before its fit came back, on a reopen, and
// on entering the 3-D view of a z-stack (tests/test_browser_contrast.py).
//
//   website  the static site + the website's Python bundle (serve.py); the
//            images picked from this computer, imported as the Import dialog
//            imports them.
//   desktop  this checkout's Plexora in CPython (tools/bench3d/serve.py, a data
//            root of its own), the images imported by their paths.
//
// Each arm imports the image (project A) and a copy of it (project B), and:
//
//   first     opens A and watches every channel slot of the sidebar every
//             100 ms until each one on screen has had its fit put on it and
//             nothing moved for a second; then switches every other channel
//             on and watches again. Every window auto-levelling put on a slot
//             is logged (the sidebar's applyAutoRange), so both passes are
//             seen however fast the second follows the first.
//   left      opens B and leaves it 300 ms later -- before its contrast has
//             landed -- then opens it again: the fit must land on the reopen.
//   volume    (--volume, a z-stack) enters A's 3-D view and reads what the
//             renderer draws each channel with.
//   reopen    (--reopen N) opens B, then A again, N times: A's fits come from
//             the store beside the project (`kept`); with --before, each
//             round also does the same once with A's stored fits deleted
//             first (`forgot`): the reopen as it was before the store.
//
//   PLEXORA_BENCH_HOME=<bundle home> node tests/browser/contrast.mjs --out DIR \
//       [--files IMAGE] [--arms website,desktop] [--volume] [--reopen N] [--before] [--headed]
//
// Writes DIR/record.json (checks per arm and between arms); prints it; exits 1
// when a check fails. Without --files the image is tests/browser/
// smoke_fixture.py's (two uint16 channels, 1024 x 768).
import { execFileSync, spawnSync } from "node:child_process";
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, seedBrowserStore, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const outDir = path.resolve(opt("--out", mkdtempSync(path.join(tmpdir(), "pxcon-out-"))));
const arms = opt("--arms", "website,desktop").split(",").filter(Boolean);
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));
const settleMs = Number(opt("--settle-ms", "120000"));
const enterVolume = args.includes("--volume");
const reopens = Number(opt("--reopen", "0"));
const before = args.includes("--before");
const headed = args.includes("--headed");
mkdirSync(outDir, { recursive: true });

const temp = mkdtempSync(path.join(tmpdir(), "pxcon-"));
let image = opt("--files", "") ? path.resolve(opt("--files", "")) : null;
if (!image) {
    image = execFileSync(PYTHON, [path.join(REPO, "tests", "browser", "smoke_fixture.py"), path.join(temp, "fixture")],
                         { cwd: REPO, encoding: "utf-8" }).trim().split("\n").pop().trim();
}
// Project B: the same image under another name (a folder for an OME-Zarr).
const copyDir = path.join(temp, "copy");
mkdirSync(copyDir, { recursive: true });
const ext = path.basename(image).slice(path.basename(image).indexOf("."));
const copy = path.join(copyDir, `other${ext}`);
if (statSync(image).isDirectory()) cpSync(image, copy, { recursive: true });
else copyFileSync(image, copy);
const files = [image, copy];
const env = { ...process.env, PYTHONPATH: REPO };
const record = { image: path.basename(image), volume: enterVolume, arms: {}, checks: {} };
const log = (text) => process.stderr.write(`${text}\n`);

/** Every file under `dir`, relative, with forward slashes. */
function walk(dir, prefix = "") {
    const found = [];
    for (const entry of readdirSync(dir)) {
        const full = path.join(dir, entry);
        if (statSync(full).isDirectory()) found.push(...walk(full, `${prefix}${entry}/`));
        else found.push(`${prefix}${entry}`);
    }
    return found;
}

// -- the page's side ----------------------------------------------------------

/**
 * Installed in every document: the stats and fit requests the page makes
 * (`__pxconAsked`), and every window auto-levelling puts on a slot
 * (`__pxconApplied`: the sidebar's applyAutoRange, wrapped on the instance).
 */
function instrument() {
    if (window.__pxconAsked) return;
    window.__pxconAsked = [];
    window.__pxconApplied = [];
    const native = window.fetch;
    window.fetch = function (input, init) {
        const url = String(input && input.url ? input.url : input);
        const match = /(get_image_channel_stats|get_channel_gmm)\b/.exec(url);
        if (!match) return native.apply(this, arguments);
        const channel = new URL(url, location.href).searchParams.get("channel");
        const entry = { route: match[1], channel, at: Math.round(performance.now()) };
        window.__pxconAsked.push(entry);
        return native.apply(this, arguments).then((response) => {
            entry.ms = Math.round(performance.now() - entry.at);
            entry.status = response.status;
            response.clone().json().then((body) => {
                if (body && body.vmin !== undefined) entry.fit = [body.vmin, body.vmax];
                if (body && body.vmin_hint !== undefined) entry.hint = [body.vmin_hint, body.vmax_hint];
            }).catch(() => null);
            return response;
        });
    };
    const wrap = () => {
        const sidebar = window.__plexora?.viewerSidebar;
        if (!sidebar || typeof sidebar.applyAutoRange !== "function") { setTimeout(wrap, 5); return; }
        const apply = sidebar.applyAutoRange;
        sidebar.applyAutoRange = function (index, slot, raw, packet) {
            window.__pxconApplied.push({ name: slot.name, raw: [Number(raw[0]), Number(raw[1])],
                                         at: Math.round(performance.now()) });
            return apply.apply(this, arguments);
        };
    };
    wrap();
}

/** What every channel slot holds now; raw windows. */
function slotState() {
    const sidebar = window.__plexora?.viewerSidebar;
    if (!sidebar) return null;
    const fits = sidebar.channelList?.hasChannelGMM || {};
    const round = (v) => Math.round(Number(v) * 100) / 100;
    return {
        t: Math.round(performance.now()),
        hd: sidebar.isHdMode(),
        slots: sidebar.channelSlots.filter((slot) => slot && slot.name).map((slot) => {
            const fit = fits[slot.name];
            let raw = null;
            try { raw = sidebar.toRawRangeForSlot(slot).map(round); } catch { raw = null; }
            return {
                name: slot.name, enabled: Boolean(slot.enabled), raw, curve: slot.curve || null,
                source: slot.rangeSource || null, autoLeveled: Boolean(slot.autoLeveled),
                autoLeveling: Boolean(slot.autoLeveling), user: Boolean(slot.userRangeChanged),
                easing: Boolean(slot.rangeEase),
                fit: fit && fit.vmin !== undefined ? { window: [fit.vmin, fit.vmax], log: fit.log_window || null,
                                                       q: [fit.qmin, fit.qmax] } : null,
            };
        }),
    };
}

/**
 * Sample slotState (changes only) until every enabled slot has its fit on it
 * and nothing moved for `quietMs`, or `limitMs` passes.
 */
async function watchSlots([limitMs, quietMs]) {
    const read = window.__pxconState;
    const started = performance.now();
    const samples = [];
    let last = "";
    let still = 0;
    let landedAt = null;
    while (performance.now() - started < limitMs) {
        const state = read();
        const key = JSON.stringify(state && state.slots);
        if (key !== last) { samples.push(state); last = key; still = 0; } else { still += 50; }
        const on = state ? state.slots.filter((s) => s.enabled) : [];
        const landed = on.length > 0 && on.every((s) => s.autoLeveled && !s.autoLeveling && !s.easing && s.fit
            && s.raw && Math.abs(s.raw[0] - s.fit.window[0]) <= 1 && Math.abs(s.raw[1] - s.fit.window[1]) <= 1);
        if (landed && landedAt === null) landedAt = state.t;
        if (!landed) landedAt = null;
        if (landed && still >= quietMs) return { samples, settled: true, landedAt, ms: Math.round(performance.now() - started) };
        await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return { samples, settled: false, landedAt, ms: Math.round(performance.now() - started) };
}

// -- the two arms -------------------------------------------------------------

async function websiteArm(browser) {
    const server = await startServer(["--site", site]);
    const page = await browser.context.newPage();
    const base = `http://127.0.0.1:${server.port}`;
    await seedBrowserStore(browser.context, base);
    await page.goto(`${base}/?pydebug`);
    let frame = await appFrame(page);
    await page.evaluate(() => window.PlexoraPython.ready);
    const paths = [];
    for (const [i, file] of files.entries()) {
        const folder = statSync(file).isDirectory();
        const inside = folder ? walk(file) : [path.basename(file)];
        await page.evaluate((id) => {
            const input = document.createElement("input");
            input.type = "file"; input.multiple = true; input.id = id; input.hidden = true;
            document.body.append(input);
        }, `con-pick-${i}`);
        await page.setInputFiles(`#con-pick-${i}`, folder ? inside.map((p) => path.join(file, p)) : [file]);
        paths.push(await page.evaluate(async ([id, isFolder, rels, name]) => {
            const picked = [...document.getElementById(id).files];
            const root = `/picked/con${id.split("-").pop()}`;
            if (!isFolder) {
                const [mounted] = await window.PlexoraPython.mount(root, picked);
                window.PlexoraPicks.adopt(mounted, picked[0]);
                return mounted;
            }
            const blobs = picked.map((data, k) => ({ name: `${name}/${rels[k]}`, data }));
            await window.PlexoraPython.mountTree(root, blobs);
            for (const blob of blobs) window.PlexoraPicks.adopt(`${root}/${blob.name}`, blob.data);
            return `${root}/${name}`;
        }, [`con-pick-${i}`, folder, inside, path.basename(file)]));
    }
    return {
        page, frame, paths,
        open: async (name) => {
            await frame.evaluate((n) => { location.href = `/${encodeURIComponent(n)}`; }, name);
            frame = await appFrame(page);
            return frame;
        },
        // The stored fits, deleted in the worker's own copy of the project.
        forget: (name) => page.evaluate((code) => window.PlexoraPython.run(code), [
            "from plexora.server.models import database_model as _d",
            `_d.save_list(_d.ChannelFit, datasource=${JSON.stringify(name)}, cells=b'{}')`,
            "'forgot'"].join("\n")),
        close: () => server.stop(),
    };
}

async function desktopArm(browser) {
    const data = path.join(outDir, "desktop-data");
    const started = spawnSync(PYTHON, [path.join(REPO, "tools", "bench3d", "serve.py"), "--data", data],
                              { cwd: REPO, encoding: "utf8", env });
    if (started.status !== 0) throw new Error(`desktop server: ${started.stderr.slice(-2000)}`);
    const server = JSON.parse(started.stdout.trim().split("\n").pop());
    const page = await browser.context.newPage();
    const base = `http://127.0.0.1:${server.port}`;
    await page.goto(`${base}/`);
    return {
        page, frame: page, paths: files,
        open: async (name) => { await page.goto(`${base}/${encodeURIComponent(name)}`); return page; },
        forget: (name) => spawnSync(PYTHON, ["-c", [
            "import os, sys",
            "from plexora.server.models import database_model as d",
            "d.save_list(d.ChannelFit, datasource=sys.argv[1], cells=b'{}')"].join("\n"), name],
            { cwd: REPO, env: { ...env, PLEXORA_DATA_PATH: data } }).status,
        close: () => spawnSync(PYTHON, [path.join(REPO, "tools", "bench3d", "serve.py"), "--stop", String(server.pid)],
                               { cwd: REPO, env }),
    };
}

const viewerFull = (frame) => frame.waitForFunction(() => {
    const viewer = window.__plexora?.seaDragonViewer?.viewer;
    if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
    for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
        if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
    }
    return true;
}, null, { timeout: 300000, polling: 100 });

const final = (watched) => (watched.samples.length ? watched.samples[watched.samples.length - 1] : null);

/** One open of a project's viewer, watched until its contrast has settled. */
async function openViewer(arm, name) {
    const t0 = Date.now();
    const frame = await arm.open(name);
    await frame.waitForFunction(() => window.__plexora?.viewerSidebar?.channelSlots, null,
                                { timeout: 120000, polling: 20 });
    const watching = frame.evaluate(watchSlots, [settleMs, 1000]);
    const out = {};
    await viewerFull(frame).catch(() => { out.planeTimedOut = true; });
    out.drawnMs = Date.now() - t0;
    const watched = await watching;
    Object.assign(out, { settled: watched.settled, landedAt: watched.landedAt, samples: watched.samples,
                         final: final(watched) });
    return { frame, out };
}

const readLogs = (frame) => frame.evaluate(() => ({ asked: window.__pxconAsked || [],
                                                    applied: window.__pxconApplied || [] }));

async function runArm(kind) {
    const browser = await launch({ headed });
    const out = { errors: [] };
    let arm = null;
    try {
        // As a script, so the page's Content-Security-Policy (no eval on the
        // website) never sees a string turned into code.
        await browser.context.addInitScript({ content: `(${instrument})(); window.__pxconState = ${slotState};` });
        arm = kind === "website" ? await websiteArm(browser) : await desktopArm(browser);
        arm.page.on("pageerror", (error) => out.errors.push(`pageerror: ${String(error.stack || error).slice(0, 600)}`));
        await arm.page.setViewportSize({ width: 1440, height: 900 });
        const names = [];
        for (const pick of arm.paths) {
            const imported = await arm.frame.evaluate(async (p) => {
                const post = async (route, body) => {
                    const response = await fetch(route, { method: "POST", headers: { "Content-Type": "application/json" },
                                                          body: JSON.stringify(body) });
                    return { status: response.status, body: await response.json().catch(() => null) };
                };
                await post("/import/inspect", { paths: [p] });
                return post("/import/sample", { paths: [p], answers: {} });
            }, pick);
            const name = imported.body && imported.body.name;
            if (!name) throw new Error(`import failed: ${JSON.stringify(imported).slice(0, 600)}`);
            names.push(name);
        }
        const [A, B] = names;
        out.projects = names;
        log(`${kind}: imported ${names.join(", ")}`);

        // -- first: A's first open, then every channel switched on.
        let { frame, out: first } = await openViewer(arm, A);
        const others = await frame.evaluate(() => {
            const sidebar = window.__plexora.viewerSidebar;
            const on = new Set(sidebar.channelSlots.filter((s) => s.enabled && s.name).map((s) => s.name));
            const missing = (sidebar.columns || []).filter((name) => !on.has(name));
            const switched = [];
            for (const slot of sidebar.channelSlots) {
                if (slot.name && !slot.enabled) { sidebar.setSlotEnabled(slot.index, true); switched.push(slot.name); }
            }
            return { switched, missing };
        });
        const all = await frame.evaluate(watchSlots, [settleMs, 1000]);
        first.allChannels = { switched: others.switched, settled: all.settled, final: final(all) };
        Object.assign(first, await readLogs(frame));
        out.first = first;
        log(`${kind}: first open settled=${first.settled}, all channels settled=${all.settled} `
            + JSON.stringify(final(all)?.slots.map((s) => [s.name, s.raw])));

        // -- volume: A's 3-D view.
        if (enterVolume) {
            out.volume = await frame.evaluate(async ([limitMs]) => {
                const P = window.__plexora;
                const t = performance.now();
                P.viewState.set({ mode: "volume", layout: "single" });
                let status = null;
                while (performance.now() - t < limitMs) {
                    const r = P.volumeView?.renderer;
                    status = r?.status || null;
                    if (r && r.status === "ready" && !r._loads?.size) break;
                    if (r && ["unavailable", "error"].includes(r.status)) break;
                    await new Promise((resolve) => setTimeout(resolve, 20));
                }
                const read = window.__pxconState;
                await new Promise((resolve) => setTimeout(resolve, 1500));
                const r = P.volumeView?.renderer;
                let channels = null;
                try { channels = (r?.channels?.() || []).map((c) => ({ name: c.name, raw: c.raw, curve: c.curve || null })); }
                catch (error) { channels = String(error); }
                return { status, enteredMs: Math.round(performance.now() - t), renderer: channels, slots: read() };
            }, [settleMs]);
            log(`${kind}: 3-D ${out.volume.status} ${JSON.stringify(out.volume.renderer)}`);
        }

        // -- left: B left 300 ms after it opened, then opened again.
        {
            const leftFrame = await arm.open(B);
            await leftFrame.waitForFunction(() => window.__plexora?.viewerSidebar?.channelSlots, null,
                                            { timeout: 120000, polling: 20 });
            await leftFrame.waitForTimeout(300);
            const leftAt = await leftFrame.evaluate(slotState);
            ({ frame } = await openViewer(arm, A));   // somewhere else
            const again = await openViewer(arm, B);
            out.left = { leftAt, settled: again.out.settled, final: again.out.final, ...(await readLogs(again.frame)) };
            frame = again.frame;
            log(`${kind}: left early, reopened settled=${again.out.settled} `
                + JSON.stringify(again.out.final?.slots.filter((s) => s.enabled).map((s) => [s.name, s.raw])));
        }

        // -- reopen: B, then A again, timed; with --before also with A's fits forgotten.
        out.reopens = [];
        for (let round = 0; round < reopens; round += 1) {
            for (const mode of before ? (round % 2 ? ["forgot", "kept"] : ["kept", "forgot"]) : ["kept"]) {
                await openViewer(arm, B);
                if (mode === "forgot") await arm.forget(A);
                const { frame: f, out: o } = await openViewer(arm, A);
                const logs = await readLogs(f);
                const fits = logs.asked.filter((a) => a.route === "get_channel_gmm");
                out.reopens.push({ round, mode, settled: o.settled, landedAt: o.landedAt, drawnMs: o.drawnMs,
                                   fitMs: fits.map((a) => a.ms), final: o.final });
                log(`${kind}: reopen ${round} ${mode}: landed at ${o.landedAt} ms, fits ${JSON.stringify(fits.map((a) => a.ms))}`);
            }
        }
    } catch (error) {
        out.error = String(error.stack || error).slice(0, 1500);
        log(`${kind}: ${out.error}`);
    } finally {
        await browser.close();
        arm?.close();
    }
    return out;
}

for (const kind of arms) record.arms[kind] = await runArm(kind);

// -- the checks -----------------------------------------------------------------

const check = (name, ok, detail) => { record.checks[name] = { ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) }; };
const near = (a, b, tol = 0.51) => Array.isArray(a) && Array.isArray(b) && a.length === b.length
    && a.every((v, i) => Math.abs(Number(v) - Number(b[i])) <= tol);
const enabled = (state) => (state?.slots || []).filter((s) => s.enabled);
/** Each enabled channel's window is its fit's. */
const onFits = (state) => enabled(state).length > 0
    && enabled(state).every((s) => s.fit && s.autoLeveled && near(s.raw, s.fit.window, 1));

for (const [kind, arm] of Object.entries(record.arms)) {
    check(`${kind}: ran`, !arm.error, arm.error);
    if (arm.error) continue;
    const first = arm.first;
    // Both passes on every channel: the statistics' hint, then the fit, each
    // put on the slot (the applyAutoRange log), the fit last.
    const passes = {};
    for (const s of enabled(first.allChannels.final)) {
        const hint = first.asked.find((a) => a.route === "get_image_channel_stats" && a.hint
                                      && (a.channel === s.name || a.channel?.endsWith(s.name)))?.hint;
        const put = first.applied.filter((a) => a.name === s.name).map((a) => a.raw);
        passes[s.name] = { hint, put, fit: s.fit?.window,
                           ok: Boolean(hint && s.fit && put.some((r) => near(r, hint))
                                       && near(put[put.length - 1], s.fit.window)) };
    }
    check(`${kind}: the hint then the fit landed on every channel`,
          Object.keys(passes).length >= 2 && Object.values(passes).every((p) => p.ok), passes);
    check(`${kind}: the first open settled on its fits`, first.settled && onFits(first.final), first.final);
    check(`${kind}: every channel settled on its fit`, first.allChannels.settled && onFits(first.allChannels.final),
          first.allChannels.final);
    check(`${kind}: a viewer left before its fit landed reopens on the fit`,
          arm.left.settled && onFits(arm.left.final), { leftAt: arm.left.leftAt, final: arm.left.final });
    for (const r of arm.reopens) {
        check(`${kind}: reopen ${r.round} (${r.mode}) settled on the fits`, r.settled && onFits(r.final), r.final);
    }
    if (enterVolume) {
        const v = arm.volume;
        const drawn = Array.isArray(v.renderer) ? v.renderer : [];
        const ok = v.status === "ready" && enabled(v.slots).length > 0 && enabled(v.slots).every((s) => {
            const r = drawn.find((c) => c.name === s.name || c.name?.endsWith(s.name) || s.name.endsWith(c.name));
            return r && s.fit && near(r.raw, s.fit.window, 1) && near(s.raw, s.fit.window, 1);
        });
        check(`${kind}: the 3-D view draws every channel at its fit`, ok, { status: v.status, renderer: v.renderer,
                                                                           slots: enabled(v.slots) });
    }
}

const [web, desk] = [record.arms.website, record.arms.desktop];
if (web && desk && !web.error && !desk.error) {
    /** {channel: {window, fit, log}} of a state, by channel name. */
    const windows = (state) => Object.fromEntries(enabled(state).map((s) => [s.name, { window: s.raw, fit: s.fit?.window,
                                                                                       log: s.fit?.log }]));
    // The Log window is not rounded: its last digits are each libm's exp
    // (12111.9436001609 on the website, ...0990 on the desktop).
    const sameLog = (a, b) => (!a && !b) || Boolean(a && b
        && Math.abs(a.cofactor - b.cofactor) <= 1e-6 * Math.abs(b.cofactor)
        && a.window.every((v, i) => Math.abs(v - b.window[i]) <= 1e-6 * Math.max(1, Math.abs(b.window[i]))));
    const same = (label, a, b) => {
        const [wa, wb] = [windows(a), windows(b)];
        const names = [...new Set([...Object.keys(wa), ...Object.keys(wb)])];
        const ok = names.length > 0 && names.every((n) => wa[n] && wb[n] && near(wa[n].window, wb[n].window, 0.01)
            && near(wa[n].fit, wb[n].fit, 0) && sameLog(wa[n].log, wb[n].log));
        check(`both apps: ${label}`, ok, { website: wa, desktop: wb });
    };
    same("the first open's windows are the same", web.first.final, desk.first.final);
    same("every channel's window is the same", web.first.allChannels.final, desk.first.allChannels.final);
    same("the reopen after leaving early has the same windows", web.left.final, desk.left.final);
    if (enterVolume) {
        const drawn = (arm) => (Array.isArray(arm.volume.renderer) ? arm.volume.renderer : [])
            .map((c) => [c.name, (c.raw || []).map(Number)]);
        const [dw, dd] = [drawn(web), drawn(desk)];
        check("both apps: the 3-D view draws the same windows",
              dw.length > 0 && dw.length === dd.length && dw.every(([n, r], i) => dd[i][0] === n && near(r, dd[i][1], 0.01)),
              { website: dw, desktop: dd });
    }
}
record.ok = Object.values(record.checks).every((c) => c.ok);
writeFileSync(path.join(outDir, "record.json"), JSON.stringify(record, null, 1));
console.log(JSON.stringify({ ok: record.ok, checks: Object.fromEntries(Object.entries(record.checks).map(([k, v]) => [k, v.ok])) }, null, 1));
if (!record.ok) process.exitCode = 1;
