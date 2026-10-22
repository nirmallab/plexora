/**
 * Where the fast tooltip goes, so that it is never cut off.
 *
 * services/tooltip.js places one bubble in window coordinates from the
 * control's rectangle. This drives its pure `place()` with numbers:
 *
 *   1. Above by default, centred, the tail on the control's centre.
 *   2. No room above (a control at the top of the window) -> below.
 *   3. Asked for below with no room below -> above.
 *   4. A control at the window's edge slides the bubble in, and the tail
 *      still points at the control, never off the bubble's rounded corner.
 *   5. align="right" hangs the bubble left from the control's right edge.
 *
 * Run directly:  node tests/js/tooltip_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/tooltip.js");

// No document: the listeners are not hung, and place() is all there is.
const context = createContext({ window: {} });
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: SOURCE });
const { place } = context.window.PlexoraTooltip;

let failures = 0;
function check(ok, message) {
    if (ok) console.log(`ok - ${message}`);
    else { failures += 1; console.log(`FAIL - ${message}`); }
}

const view = { width: 1200, height: 800 };
const size = { width: 200, height: 40 };
const box = (left, top, width = 40, height = 30) =>
    ({ left, top, width, height, right: left + width, bottom: top + height });

{
    const r = box(500, 400);
    const s = place(r, size, view);
    check(s.side === "top" && s.top + size.height <= r.top
          && s.left === 420 && s.tailX === 100,
          "above by default, centred on the control, tail at its centre");
}

{
    const r = box(500, 10);
    const s = place(r, size, view);
    check(s.side === "bottom" && s.top >= r.bottom && s.top >= 0,
          "no room above, so it goes below rather than off the top");
}

{
    const r = box(500, 770);
    const s = place(r, size, view, { placement: "bottom" });
    check(s.side === "top" && s.top + size.height <= r.top
          && s.top + size.height <= view.height,
          "asked for below with no room there, so it goes above");
}

{
    const r = box(4, 400, 24, 24);
    const s = place(r, size, view);
    const centre = r.left + r.width / 2;
    check(s.left >= 8 && s.left + s.tailX >= centre - 0.5 && s.tailX >= 16,
          "at the left edge it slides in, and the tail still points at the control");
}

{
    const r = box(1180, 400, 16, 24);
    const s = place(r, size, view);
    check(s.left + size.width <= view.width - 8 && s.tailX <= size.width - 16,
          "at the right edge it slides in, the tail kept off the rounded corner");
}

{
    const r = box(900, 400);
    const s = place(r, size, view, { align: "right" });
    check(s.left + size.width === r.right + 4,
          "align right hangs it left from the control's right edge");
}

{
    const r = box(500, 400);
    const wide = { width: 1400, height: 40 };
    const s = place(r, wide, view);
    check(s.left === 8, "a bubble wider than the window starts at the margin");
}

if (failures) {
    console.log(`${failures} failed`);
    process.exit(1);
}
console.log("all tooltip placement checks passed");
