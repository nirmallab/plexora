// Where Python's boot time goes: loadPyodide, then each base package on its
// own (in stage order), then the wheels unpack, then `import plexora` +
// create_app, all against the published bundle. Warm HTTP cache (second
// pass in the same profile) so the numbers are CPU, not download.
//   node bench_packages.mjs
import { chromium, CHROME, PY, REPO, PYTHONPATH } from "./benchlib.mjs";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const BASE = "https://assets.plexoraapp.com/py/0.0.41/";
const profile = mkdtempSync(path.join(tmpdir(), "pxpkg-"));
const ctx = await chromium.launchPersistentContext(profile, { executablePath: CHROME, headless: true, args: ["--no-first-run"] });

const RUN = async (base) => {
    const t = () => performance.now();
    const out = { steps: [] };
    const stages = await (await fetch(`${base}stages.json`)).json();
    let t0 = t();
    const { loadPyodide } = await import(`${base}pyodide/pyodide.mjs`);
    const py = await loadPyodide({ indexURL: `${base}pyodide/` });
    out.steps.push(["loadPyodide", Math.round(t() - t0)]);
    for (const name of stages.base.pyodide) {
        t0 = t();
        try { await py.loadPackage(name, { messageCallback: () => {} }); } catch (e) { out.steps.push([`${name} FAILED`, String(e).slice(0, 80)]); continue; }
        out.steps.push([name, Math.round(t() - t0)]);
    }
    t0 = t();
    const site = py.runPython("import site; site.getsitepackages()[0]");
    const buffers = await Promise.all(stages.base.wheels.map(async (w) => [w, await (await fetch(`${base}wheels/${w}`)).arrayBuffer()]));
    out.steps.push(["wheels fetch", Math.round(t() - t0)]);
    t0 = t();
    for (const [w, buf] of buffers) py.unpackArchive(buf, "wheel", { extractDir: site });
    out.steps.push(["wheels unpack", Math.round(t() - t0)]);
    const imp = async (code, label) => { t0 = t(); try { await py.runPythonAsync(code); out.steps.push([label, Math.round(t() - t0)]); } catch (e) { out.steps.push([`${label} FAILED`, String(e).slice(0, 300)]); } };
    await imp("import numpy", "import numpy");
    await imp("import scipy, scipy.ndimage, scipy.spatial", "import scipy(+ndimage,spatial)");
    await imp("import sklearn.mixture", "import sklearn.mixture");
    await imp("import polars", "import polars");
    await imp("import zarr, numcodecs", "import zarr,numcodecs");
    await imp("import PIL.Image", "import PIL");
    await imp("import tifffile", "import tifffile");
    await imp("import flask, jinja2", "import flask");
    await imp("import pydantic, cryptography", "import pydantic,cryptography");
    await imp(`
import os
os.environ.update({"PLEXORA_DATA_PATH": "/data", "PLEXORA_BACKEND": "browser", "PLEXORA_WORK": "cooperative", "PLEXORA_NO_NUMBA": "1", "BIOCOGNIA_DIR": "/bioc", "BIOCOGNIA_NO_HEARTBEAT": "1", "PLEXORA_TELEMETRY": "off", "PLEXORA_ALLOWED_ORIGINS": "https://x"})
os.makedirs("/data", exist_ok=True); os.makedirs("/bioc", exist_ok=True)
from plexora.browser import boot
boot.codecs()
import plexora.server.models.data_model`, "import plexora.server.models.data_model (+codecs shim)");
    await imp("import plexora; plexora.app", "plexora.app (create_app, all plugins)");
    await imp("import spatialagents, spatialbridge, biocognia", "import spatialagents,spatialbridge,biocognia");
    out.heapMB = Math.round(py._module.HEAP8.length / 1e6);
    out.modules = py.runPython("import sys; len(sys.modules)");
    return out;
};

const page = await ctx.newPage();
await page.goto("https://viewer.plexoraapp.com/selfcheck");
// first pass warms the cache
const cold = await page.evaluate(RUN, BASE);
console.log(JSON.stringify({ pass: "cold", ...cold }));
const page2 = await ctx.newPage();
await page2.goto("https://viewer.plexoraapp.com/selfcheck");
const warm = await page2.evaluate(RUN, BASE);
console.log(JSON.stringify({ pass: "warm", ...warm }));
await ctx.close();
