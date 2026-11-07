// The website's Python past 2 GiB of WebAssembly heap. Pyodide 314.0.7's own
// glue read four addresses as signed numbers (tools/browser_build/build.py
// GLUE_FIXES): once the heap passed 2 GiB a buffer copied into Python threw
// "offset is out of bounds", and bytes or a string of 64+ characters handed
// to the page came from the wrong end of memory -- a 60 M-value table then
// answered every JSON route with its own column values. Here the heap is
// pushed past 2 GiB with ballast and each crossing is checked at an address
// above 2^31:
//   - a JS Float64Array into Python (to_py: JsvBuffer_assignToPtr),
//   - a Python buffer into a JS one (assign: JsvBuffer_assignFromPtr),
//   - a numpy array to the page (to_js: python2js_buffer_1d_contiguous),
//   - a long ASCII string to the page (_python2js_ascii),
//   - and a JSON route through the request bridge.
//
//   node tests/browser/heap_2g.mjs [--site DIR] [--py DIR] [--ballast MIB]
// (--py: another bundle's py/ folder, for serve.py)
// Prints JSON with `ok` and the checks.
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const ballastMib = Number(opt("--ballast", "2304"));
let site = opt("--site", null);
if (!site) {
    site = mkdtempSync(path.join(tmpdir(), "pxheap-site-"));
    execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site],
                 { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, stdio: ["ignore", "ignore", "inherit"] });
}
const out = { ballastMib, checks: [], errors: [] };
const check = (name, ok, detail = {}) => {
    out.checks.push({ name, ok: Boolean(ok), ...detail });
    console.error(`${ok ? "ok  " : "FAIL"} ${name}`);
};

const server = await startServer(["--site", site, ...(opt("--py", null) ? ["--py", opt("--py", null)] : [])]);
const browser = await launch({});
try {
    const page = await browser.context.newPage();
    page.on("pageerror", (error) => out.errors.push(String(error).slice(0, 300)));
    await page.goto(`http://127.0.0.1:${server.port}/?pydebug`);
    const frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    // Python keeps `held` (the ballast) and `py` (what the page reads back) as
    // module globals between the calls below.
    const result = JSON.parse(await page.evaluate((mib) => window.PlexoraPython.run(`
import json, js
import numpy as np
from pyodide.ffi import to_js
import __main__ as m
m.held = [np.ones(64 * 2**20, dtype=np.uint8) for _ in range(${Math.ceil(mib / 64)})]
out = {"high": 2**31}
# Into Python: a JS array's bytes copied to an address past 2^31.
arr = js.Float64Array.new(1_000_000)
arr.fill(123.25)
try:
    x = np.frombuffer(arr.to_py(), dtype="<f8")
    out["to_py"] = {"ok": bool((x == 123.25).all()), "addr": int(x.__array_interface__["data"][0])}
except Exception as error:
    out["to_py"] = {"ok": False, "error": repr(error)[:200]}
# Into a JS buffer from Python memory past 2^31.
src = np.arange(1_000_000, dtype="<f8") * 0.5
target = js.Float64Array.new(1_000_000)
try:
    target.assign(src)
    out["assign"] = {"ok": float(target[999_999]) == 499_999.5 and float(target[3]) == 1.5,
                     "addr": int(src.__array_interface__["data"][0])}
except Exception as error:
    out["assign"] = {"ok": False, "error": repr(error)[:200]}
# To the page: a numpy array and a long string, kept for the page to read.
m.py_array = np.arange(1_000_000, dtype="<f8") * 0.25
m.py_text = "plexora-" * 1_000_000
out["array_addr"] = int(m.py_array.__array_interface__["data"][0])
out["text_addr"] = id(m.py_text)
m.js_array = to_js(m.py_array)
json.dumps(out)
`), ballastMib));
    out.heapMib = Math.round(await page.evaluate(() => window.PlexoraPython.heap()) / 2 ** 20);
    check("the heap is past 2 GiB", out.heapMib > 2048, { heapMib: out.heapMib });
    check("a JS array copied into Python past 2^31 arrives whole (to_py)", result.to_py.ok
          && result.to_py.addr >= result.high, result.to_py);
    check("Python memory past 2^31 copied into a JS array arrives whole (assign)", result.assign.ok
          && result.assign.addr >= result.high, result.assign);
    const fromPython = await page.evaluate(async () => {
        const arrayOk = await window.PlexoraPython.run(`
import __main__ as m
a = m.js_array
str(a.length == 1_000_000 and a[3] == 0.75 and a[999_999] == 249_999.75)
`);
        const text = await window.PlexoraPython.run("import __main__ as m\nm.py_text");
        return { arrayOk, textOk: text === "plexora-".repeat(1_000_000), textLength: text.length };
    });
    check("a numpy array past 2^31 reaches the page whole (to_js)", fromPython.arrayOk === "True"
          && result.array_addr >= result.high, { addr: result.array_addr, answer: fromPython.arrayOk });
    check("a long string past 2^31 reaches the page whole", fromPython.textOk && result.text_addr >= result.high,
          { addr: result.text_addr, length: fromPython.textLength });
    const routes = await frame.evaluate(async () => {
        const got = {};
        for (const route of ["config", "get_load_status?datasource=none", "health"]) {
            const response = await fetch(route);
            const text = await response.text();
            let parsed = true;
            if (text) { try { JSON.parse(text); } catch (error) { parsed = false; } }
            got[route] = { status: response.status, parsed, head: text.slice(0, 40) };
        }
        return got;
    });
    check("JSON routes through the bridge still parse", Object.values(routes).every((r) => r.parsed), { routes });
    check("no page errors", !out.errors.length, { errors: out.errors });
} catch (error) {
    out.checks.push({ name: "the run finished", ok: false, error: String(error.message || error).slice(0, 600) });
} finally {
    await browser.close();
    server.stop();
}
out.ok = out.checks.every((c) => c.ok);
console.log(JSON.stringify(out, null, 1));
process.exitCode = out.ok ? 0 : 1;
