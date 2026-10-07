/**
 * Does a launch link's context reach the viewer's own handlers?
 *
 * services/launchContext.js applies `window.flaskVariables.launch` -- the
 * colour-by column, the viewport, the cells to point at, the regions to
 * outline -- through `PlexoraAgentBridge.run`, the same handlers an agent's
 * commands reach. Nothing in the Python suite runs it, so this loads it into a
 * stand-in page and records what it asked the bridge for.
 *
 * Cases, each a fresh page:
 *   full     every field, no tool open: four runs, in order, with the
 *            arguments the handlers take (regions sticky: ttl_ms 0)
 *   tool     another tool holds the cell layer: colour-by is skipped
 *   empty    no context: nothing runs at all
 *   failing  one step throws: the rest still run
 *
 * Run directly: node tests/js/launch_context_probe.mjs
 * Prints one JSON report on stdout; exit 0 when every case matched.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/services/launchContext.js"), "utf8");

async function page(launch, { activeTool = "", failOn = null } = {}) {
    const runs = [];
    const window = {
        flaskVariables: { launch, active_tool: activeTool },
        __plexoraReady: Promise.resolve(),
        PlexoraAgentBridge: {
            async run(type, args) {
                runs.push({ type, args });
                if (type === failOn) throw new Error(`${type} refused`);
                return { ok: true };
            },
        },
    };
    const errors = [];
    const ctx = createContext({
        window, Promise, Array, Object, Boolean, JSON, Error,
        console: { error: (...parts) => errors.push(parts.map(String).join(" ")), log() {} },
        document: { readyState: "complete", addEventListener() {} },
    });
    runInContext(SOURCE, ctx, { filename: "launchContext.js" });
    for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve));
    return { runs, errors };
}

const report = { cases: {}, problems: [] };
const full = {
    overlay: "phenotype",
    viewport: { x: 10, y: 20, width: 100, height: 80 },
    highlight: [{ id: 3, x: 40, y: 50 }],
    regions: [{ id: "launch_0", geometry: { type: "Polygon", coordinates: [[[0, 0], [5, 0], [5, 5], [0, 0]]] }, label: "edge" }],
};

const a = await page(full);
report.cases.full = a.runs.map((run) => run.type);
const expected = ["set_color_by", "fit_region", "highlight_cells", "show_shapes"];
if (JSON.stringify(report.cases.full) !== JSON.stringify(expected)) {
    report.problems.push(`full: ran ${report.cases.full}, expected ${expected}`);
}
const byType = Object.fromEntries(a.runs.map((run) => [run.type, run.args]));
if (byType.set_color_by?.column !== "phenotype") report.problems.push("colour-by lost its column");
if (byType.show_shapes?.ttl_ms !== 0) report.problems.push("regions were not sticky (ttl_ms 0)");
if (byType.highlight_cells?.cells?.[0]?.id !== 3) report.problems.push("highlight lost its cells");
if (byType.fit_region?.width !== 100) report.problems.push("viewport lost its box");

const b = await page(full, { activeTool: "gating" });
report.cases.tool = b.runs.map((run) => run.type);
if (report.cases.tool.includes("set_color_by")) report.problems.push("colour-by ran with another tool open");

const c = await page({});
report.cases.empty = c.runs.map((run) => run.type);
if (c.runs.length) report.problems.push("an empty context ran something");

const d = await page(full, { failOn: "set_color_by" });
report.cases.failing = d.runs.map((run) => run.type);
if (d.runs.length !== 4) report.problems.push("a failing step stopped the rest");
if (!d.errors.length) report.problems.push("a failing step was not logged");

console.log(JSON.stringify(report));
process.exit(report.problems.length ? 1 : 0);
