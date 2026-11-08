// A document the website's loader opens gets pointer capture
// (tests/test_shell_pointer_capture.py).
//
// The website writes every page of the app into its frame with
// document.open/write (services/shell.js frameBoot). Chrome gives a document
// opened while the pointer is over its frame no pointer capture, ever:
// setPointerCapture does nothing, and every pointer event comes with
// pointerover/pointerenter as though the pointer had just come in. The viewer
// zoomed but never panned (OpenSeadragon measured every drag as 0 px).
//
// harness/pointer_capture/: a host page with a frame filling it, as the
// site's. The pointer rests over the frame while the frame is navigated to a
// loader that opens a page with a box that captures the pointer on a press;
// then a drag starts in the box and ends outside it. Three cases:
//
//   plain   opened bare: Chrome's fault, reproduced (the control: without it
//           the other two would prove nothing);
//   still   opened as the loader opens it (shell.js's own hiddenForOpen),
//           the pointer still;
//   moving  the same, the pointer moving over the frame while it loads.
//
//   node tests/browser/pointer_capture.mjs --out FILE [--headed]
import { writeFileSync } from "node:fs";
import { launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const outPath = opt("--out", null);

const CASES = [
    { name: "plain", mode: "plain", moving: false },
    { name: "still", mode: "shell", moving: false },
    { name: "moving", mode: "shell", moving: true },
];

const server = await startServer();
const origin = `http://127.0.0.1:${server.port}`;
const browser = await launch({ headed: args.includes("--headed") });
const out = { cases: {}, failed: [] };

async function settled(frame, test, arg = null, timeout = 15000) {
    const until = Date.now() + timeout;
    while (Date.now() < until) {
        const value = await frame.evaluate(test, arg).catch(() => null);
        if (value) return value;
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return null;
}

try {
    for (const { name, mode, moving } of CASES) {
        const page = await browser.context.newPage();
        await page.setViewportSize({ width: 800, height: 600 });
        await page.goto(`${origin}/tests/browser/harness/pointer_capture/host.html`);
        const frame = page.frame({ url: /frame\.html/ });
        await settled(frame, () => window.__written === true);
        // The pointer over the frame, as a user's is when they click on to
        // the next page, and left there while it loads.
        await page.mouse.move(600, 450);
        await page.mouse.move(605, 452);
        await frame.evaluate((m) => { location.href = `frame.html?mode=${m}`; }, mode);
        const until = Date.now() + 1500;
        for (let i = 0; moving && Date.now() < until; i += 1) {
            await page.mouse.move(600 + (i % 5) * 4, 450);
            await page.waitForTimeout(50);
        }
        const loaded = await settled(frame, (m) => location.search === `?mode=${m}`
            && (window.__written === true || window.__failed || null), mode);
        if (loaded !== true) {
            out.failed.push(`${name}: the page was never written (${loaded || "timed out"})`);
            await page.close();
            continue;
        }
        // A drag: pressed inside the box (100..400 x 100..300), three steps
        // inside it, two outside, released outside.
        await page.mouse.move(200, 200);
        await page.mouse.down();
        for (const [x, y] of [[220, 210], [240, 220], [260, 230], [600, 450], [650, 500]]) {
            await page.mouse.move(x, y);
        }
        await page.mouse.up();
        const events = await frame.evaluate(() => window.__events);
        const press = events.findIndex((event) => event.type === "pointerdown");
        const during = press < 0 ? [] : events.slice(press);
        out.cases[name] = {
            hiddenMs: await frame.evaluate(() => (window.__hiddenMs == null ? null : Math.round(window.__hiddenMs))),
            hasCaptureAfterSet: events.find((event) => event.type === "setPointerCapture")?.has ?? null,
            gotCapture: events.some((event) => event.type === "gotpointercapture"),
            // Each of these, during a drag that never left and came back,
            // is the pointer "arriving" again.
            entersDuringDrag: during.filter((event) => event.type === "pointerenter").length,
            // Moves outside the box that still reached it: capture at work.
            movesOutsideReached: during.filter((event) => event.type === "pointermove" && event.x > 400).length,
            events: events.map((event) => event.type).slice(0, 40),
        };
        await page.close();
    }
} catch (error) {
    out.failed.push(`run: ${String(error && error.stack ? error.stack : error).slice(0, 2000)}`);
} finally {
    await browser.close().catch(() => {});
    server.stop();
}

const text = JSON.stringify(out, null, 1);
if (outPath) writeFileSync(outPath, text);
console.log(text);
process.exitCode = out.failed.length ? 1 : 0;
