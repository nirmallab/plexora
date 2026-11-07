// plexora-kernels in the website's worker (plan L5): the WebAssembly wheel the
// base stage ships loads with the app, jit.py hands its ports out in place of
// the Python kernels, and each runs on tests/kernel_cases.py's inputs both ways
// -- the port, and the Python it ports (every kernel of every module swapped
// back to its `py_func`, as with no package) -- timed, and each run's outputs
// hashed so the two can be held to each other and to the desktop's.
//
//   node tests/browser/kernels.mjs [--size test|bench] [--repeat 3] [--python 1]
//
// Prints one JSON line: {boot, package, impl, cases: {name: {compiled:
// {first_ms, best_ms, digest}, python: {...}}}, inputs: {name: digest}}.
// `--product 1` also times four whole product operations both ways (`product`):
// a 32 x 256 x 256 block's cell surfaces (nets, smoothing, normals, measures),
// a 1024 x 1024 tile's classifier pixel features, its segmentation-QC sums and
// pairs, and 200,000 cells' neighbour counts.
// PLEXORA_BENCH_HOME names the bundle (tools/browser_build/serve.py).
import { readFileSync } from "node:fs";
import path from "node:path";
import { appFrame, launch, startServer, REPO } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const size = opt("--size", "test");
const repeat = Number(opt("--repeat", "3"));
const withPython = opt("--python", "1") !== "0";
const only = opt("--only", "");
//: Cases whose outputs come back whole (as lists), besides their digests.
const values = opt("--values", "");
const product = opt("--product", "0") !== "0";

const source = readFileSync(path.join(REPO, "tests", "kernel_cases.py"), "utf8");

// Runs in the worker's Python. `kernel_cases` is made a module from its
// source (tests/ is not in the wheel).
const SETUP = `
import sys, types, json, time, hashlib
import numpy as np
_src = ${JSON.stringify(source)}
kernel_cases = types.ModuleType("kernel_cases")
kernel_cases.__file__ = "kernel_cases.py"
exec(compile(_src, "kernel_cases.py", "exec"), kernel_cases.__dict__)
sys.modules["kernel_cases"] = kernel_cases

_digest = kernel_cases.digest
_outputs = kernel_cases.outputs

def _plain(v):
    # JSON has no NaN: it travels as null (numpy reads None back as nan).
    a = np.asarray(v)
    if a.dtype.kind == "f":
        nan = np.isnan(a)
        a = a.astype(object)
        a[nan] = None
    return a.tolist()

from plexora.server.utils import jit
import plexora_kernels
import importlib
from plexora.browser import imagecodecs_shim

_SWAPPED = []

def _python_kernels(on):
    """Every kernel as the Python it ports (on), or back to the ports: every
    module at once, since a Python kernel calls its module's other kernels
    through the module's globals."""
    if on:
        for key in plexora_kernels.KERNELS:
            module_name, name = key.rsplit(".", 1)
            module = importlib.import_module(module_name)
            current = getattr(module, name)
            if getattr(current, "py_func", None) is not None:
                _SWAPPED.append((module, name, current))
                setattr(module, name, current.py_func)
        imagecodecs_shim._lzw = lambda: imagecodecs_shim.lzw_decode_python
    else:
        while _SWAPPED:
            module, name, current = _SWAPPED.pop()
            setattr(module, name, current)
        imagecodecs_shim._lzw = _SHIM_LZW

_SHIM_LZW = imagecodecs_shim._lzw

_product = kernel_cases.product

_t = time.perf_counter()
CASES = kernel_cases.build(${JSON.stringify(size)})
BUILD_S = time.perf_counter() - _t
_only = set(${JSON.stringify(only)}.split(",")) - {""}
_values = set(${JSON.stringify(values)}.split(",")) - {""}
if _only:
    CASES = {k: v for k, v in CASES.items() if k.split("/")[0] in _only or k in _only}
INPUTS = {name: _digest([a for a in args if isinstance(a, (np.ndarray, bytes))]) for name, args in CASES.items()}
_b64(json.dumps({"build_s": round(BUILD_S, 2), "cases": len(CASES)}))
`;

const RUN = (mode) => `
_python_kernels(${mode === "python" ? "True" : "False"})
try:
    _got = kernel_cases.run(CASES, ${mode === "python" ? 1 : repeat}, None, 120.0)
finally:
    _python_kernels(False)
    imagecodecs_shim._lzw = _SHIM_LZW
_b64(json.dumps({name: {"impl": e["impl"], "first_ms": round(e["first_s"] * 1e3, 3),
                        "best_ms": round(e["best_s"] * 1e3, 3), "calls": e["calls"],
                        "digest": _digest(_outputs(e)),
                        "values": ([_plain(v) for v in _outputs(e)
                                    if not isinstance(v, (bytes, bytearray))]
                                   if name in _values else None)}
                 for name, e in _got.items()}))
`;

const server = await startServer(["--site", `${REPO}/plexora/client/dist/static`]);
const browser = await launch({});
const out = { size, repeat };
// Every answer is base64 of JSON: `run` hands back the last expression's repr.
const unquote = (text) => JSON.parse(Buffer.from(text.replace(/^'|'$/g, ""), "base64").toString("utf8"));
const B64 = ["import base64 as _b64m", "_b64 = lambda text: _b64m.b64encode(text.encode()).decode()", ""].join("\n");
try {
    const page = await browser.context.newPage();
    const opened = Date.now();
    await page.goto(`http://127.0.0.1:${server.port}/?pydebug`);
    await appFrame(page);
    await page.evaluate(() => window.PlexoraPython.ready);
    out.boot = { ready_ms: Date.now() - opened };
    const run = (code) => page.evaluate((c) => window.PlexoraPython.run(c), code);
    // Whether the app's boot imported the package already, and if not what
    // importing it costs (the wasm module compiled and linked).
    out.load = unquote(await run(B64 + [
        "import json, sys, time",
        "_was = 'plexora_kernels' in sys.modules",
        "_t = time.perf_counter()",
        "import plexora_kernels",
        "_b64(json.dumps({'at_boot': _was, 'import_ms': None if _was else round((time.perf_counter() - _t) * 1e3, 1)}))",
    ].join("\n")));
    out.package = unquote(await run(B64 + [
        "import json, sys",
        "from plexora.server.utils import jit",
        "from plexora.server.utils import label_kernels",
        "from plexora.server.surface import nets, mesh",
        "from plexora.plugins.qc.server.segqc import kernels as segqc",
        "from plexora.plugins.gating.server.autogate import kernels as autogate",
        "from plexora.server.utils import cell_features",
        "from plexora.server.volume import measure",
        "from plexora.browser import imagecodecs_shim",
        "k = jit.kernels_module()",
        "_b64(json.dumps({'version': getattr(k, '__version__', None), 'file': getattr(k, '__file__', None),",
        "  'ported_from': getattr(k, 'PORTED_FROM', None), 'numba': jit.numba_module() is not None,",
        "  'numba_imported': 'numba' in sys.modules, 'enabled': jit.enabled(),",
        "  'impl': {n: jit.implementation(f) for n, f in {'label_kernels._boundary': label_kernels._boundary,",
        "    'nets._count': nets._count, 'mesh._adjacency': mesh._adjacency, 'segqc.label_stats': segqc.label_stats,",
        "    'autogate._count': autogate._count, 'cell_features._tile_kernel': cell_features._tile_kernel,",
        "    'measure._bbox_kernel': measure._bbox_kernel, 'lzw_decode': imagecodecs_shim._lzw()}.items()}}))",
    ].join("\n")));
    out.setup = unquote(await run(B64 + SETUP));
    out.inputs = unquote(await run("_b64(json.dumps(INPUTS))"));
    out.cases = {};
    const compiled = unquote(await run(RUN("compiled")));
    for (const [name, entry] of Object.entries(compiled)) out.cases[name] = { compiled: entry };
    if (withPython) {
        const python = unquote(await run(RUN("python")));
        for (const [name, entry] of Object.entries(python)) out.cases[name].python = entry;
    }
    if (product) {
        out.product = {};
        for (const mode of withPython ? ["compiled", "python"] : ["compiled"]) {
            out.product[mode] = unquote(await run([
                `_python_kernels(${mode === "python" ? "True" : "False"})`,
                "try:", "    _p = _product()", "finally:", "    _python_kernels(False)",
                "_b64(json.dumps(_p))"].join("\n")));
        }
    }
    out.fallbacks = unquote(await run("_b64(json.dumps(plexora_kernels.FALLBACK_CALLS))"));
    out.heap = await page.evaluate(() => window.PlexoraPython.heap?.()).catch(() => null);
} catch (error) {
    out.error = String(error?.stack || error).slice(0, 3000);
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out));
