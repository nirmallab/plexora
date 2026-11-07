// The website's Python bundle booted in Node, from disk: what CI checks
// without a browser (web.yml's import_check). The boot is the worker's
// (client/src/js/workers/pythonWorker.js): the bundle's own Pyodide, the base
// stage's packages and wheels, then plexora/browser/boot.py's start(). Then:
//
//   boot      the app built (its route count), and no stage was asked for: a
//             library's optional import must not bring one in at boot;
//   imports   every module of the gating and QC plugins, the AI layer (chat
//             included) and the agent imports, once every stage is in;
//   stage_modules  each stage's own modules (cv2, skimage, h5py, ...) by
//             name: Plexora imports them lazily, so `imports` never does;
//   discover  registry.discover() registers every plugin's capabilities;
//   pixels    a calibrated OME-TIFF reports its pixel size. In the first
//             browser proof a missing pure-Python wheel made a calibrated
//             image read as uncalibrated, with no error anywhere (P9.md);
//             pixel_scale now logs it, and this fails on it.
//
// Prints one JSON line and exits 1 when a check fails. Correctness only: the
// times it prints are never asserted.
//
//   node tests/browser/import_check.mjs [--bundle DIR] [--version X.Y.Z]
//
// --bundle is build.py's py/<version>/ folder (default: under
// $PLEXORA_BENCH_HOME, as build.py writes it). The bundle's own pyodide.mjs is
// the loader: it is the `pyodide` npm package's file, at the version the
// bundle ships, so nothing else is installed.
import { readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);

//: Module trees imported in `imports` (their tests excepted), and route
//: modules outside them.
const TREES = ["plexora.plugins.gating", "plexora.plugins.qc", "plexora.ai", "plexora.agent"];
const MODULES = ["plexora.server.routes.ai_chat_routes", "plexora.server.routes.ai_routes",
                 "plexora.server.routes.agent_routes"];
//: Plugins whose capabilities `discover` must register.
const PLUGINS = ["gating", "qc"];

function bundleDir() {
    const given = opt("--bundle", null);
    if (given) return path.resolve(given);
    const home = process.env.PLEXORA_BENCH_HOME || "C:/plexora-bench";
    const version = opt("--version", null);
    if (!version) throw new Error("--bundle DIR (build.py's py/<version>/), or --version X.Y.Z");
    return path.join(home, "browser_node", "web", "py", version);
}

const bundle = bundleDir();
const stages = JSON.parse(readFileSync(path.join(bundle, "stages.json"), "utf8"));
const out = { bundle, checks: {}, failed: [] };
const fail = (check, why) => { out.failed.push(`${check}: ${why}`); };
const started = performance.now();
const ms = () => Math.round(performance.now() - started);

function installWheels(py, names) {
    const site = py.runPython("import site; site.getsitepackages()[0]");
    for (const name of names) {
        // A Uint8Array, not the Buffer readFileSync returns: Pyodide refuses
        // a typed array it does not know by name.
        const bytes = readFileSync(path.join(bundle, "wheels", name));
        py.unpackArchive(new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength), "wheel",
                         { extractDir: site });
    }
}

const BOOT = `
import json
from plexora.browser import boot, stages

_asked = []

def _refuse(name):
    # The boot must need no stage; one asked for is recorded and refused, so
    # the import that asked fails here as it would wait in a browser.
    _asked.append(name)
    raise RuntimeError(f"the boot asked for the {name} stage")

_finder = stages.install(_refuse)
try:
    _info = boot.start("http://127.0.0.1", post=lambda message: None,
                       cancelled=lambda job: False, idle=lambda seconds: None,
                       waiting=lambda: False)
    _error = None
except BaseException as exc:  # noqa: BLE001 -- reported
    import traceback
    _info, _error = None, "".join(traceback.format_exception(exc))[-3000:]
json.dumps({"info": _info, "error": _error, "asked": _asked, "importers": _finder.importers})
`;

const AFTER_STAGES = (names) => `
import importlib
from plexora.browser import boot

for _name in ${JSON.stringify(names)}:
    _finder.loaded.add(_name)
    boot.stage(_name)
importlib.invalidate_caches()
`;

const IMPORTS = `
import importlib
import json
import pkgutil
import time
import traceback

def _modules(trees, extra):
    names = []
    for tree in trees:
        package = importlib.import_module(tree)
        names.append(tree)
        for info in pkgutil.walk_packages(package.__path__, tree + "."):
            parts = info.name.split(".")
            if "tests" in parts or parts[-1].startswith(("test_", "conftest")):
                continue
            names.append(info.name)
    return sorted(set(names) | set(extra))

_imported, _failed = 0, {}
_t0 = time.perf_counter()
for _module in _modules(${JSON.stringify(TREES)}, ${JSON.stringify(MODULES)}):
    try:
        importlib.import_module(_module)
        _imported += 1
    except BaseException as exc:  # noqa: BLE001 -- reported
        _failed[_module] = "".join(traceback.format_exception_only(type(exc), exc)).strip()[-400:]
json.dumps({"imported": _imported, "failed": _failed,
            "seconds": round(time.perf_counter() - _t0, 1)})
`;

//: Each stage's own top-level modules, imported by name: Plexora imports most
//: of them lazily, inside the function that needs them, so the trees above
//: never reach them (a bundle once shipped an OpenCV whose loader could not
//: find its config.py, and every QC check failed on the website).
const STAGE_MODULES = `
import importlib
import json
import traceback

from plexora.browser.stages import STAGES

_ok, _failed = [], {}
for _stage, _spec in STAGES.items():
    for _module in _spec["modules"]:
        try:
            importlib.import_module(_module)
            _ok.append(_module)
        except BaseException as exc:  # noqa: BLE001 -- reported
            _failed[f"{_stage}:{_module}"] = "".join(
                traceback.format_exception_only(type(exc), exc)).strip()[-400:]
json.dumps({"imported": _ok, "failed": _failed})
`;

const DISCOVER = `
import json
from plexora.agent import registry

_names = registry.discover()
_by = {}
for _name in _names:
    _prefix = _name.split(".")[0] if "." in _name else "core"
    _by[_prefix] = _by.get(_prefix, 0) + 1
json.dumps({"count": len(_names), "by_prefix": _by})
`;

const PIXELS = `
import json
import logging
from types import SimpleNamespace

import numpy as np
import tifffile

# As tests/agent_fixtures.py writes a calibrated image: OME, 0.5 um/px.
_path = "/tmp/calibrated.ome.tif"
with tifffile.TiffWriter(_path, ome=True) as _tif:
    _tif.write(np.zeros((2, 256, 256), np.uint16), tile=(128, 128), subifds=1,
               metadata={"axes": "CYX", "PhysicalSizeX": 0.5, "PhysicalSizeXUnit": "\\u00b5m",
                         "PhysicalSizeY": 0.5, "PhysicalSizeYUnit": "\\u00b5m"})
    _tif.write(np.zeros((2, 128, 128), np.uint16), tile=(128, 128), subfiletype=1)

class _Said(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

from plexora.server.utils import pixel_scale

_said = _Said()
pixel_scale._log.addHandler(_said)
pixel_scale._log.setLevel(logging.DEBUG)
_project = SimpleNamespace(name="calibrated", resources={},
                           image=SimpleNamespace(pixel_size=None, pixel_size_z=None,
                                                 is_blank=False, is_volumetric=False, src=_path))
json.dumps({"size": pixel_scale.pixel_size(_project), "said": _said.lines})
`;

let py = null;
try {
    const { loadPyodide } = await import(pathToFileURL(path.join(bundle, "pyodide", "pyodide.mjs")).href);
    const printed = [];
    py = await loadPyodide({ indexURL: path.join(bundle, "pyodide") + path.sep,
                             stdout: (line) => printed.push(line), stderr: (line) => printed.push(line) });
    await py.loadPackage(stages.base.pyodide, { messageCallback: () => {} });
    installWheels(py, stages.base.wheels);
    for (const dir of ["/data", "/bioc", "/picked", "/derived"]) py.FS.mkdirTree(dir);
    out.checks.core = { ms: ms(), python: py.runPython("import sys; sys.version.split()[0]") };

    const booted = JSON.parse(await py.runPythonAsync(BOOT));
    out.checks.boot = { ms: ms(), ...booted };
    if (booted.error) fail("boot", booted.error.split("\n").filter(Boolean).slice(-1)[0]);
    else if (!(booted.info.rules > 300)) fail("boot", `${booted.info.rules} routes`);
    if (booted.asked.length) fail("boot", `asked for stage(s) ${booted.asked.join(", ")} (${JSON.stringify(booted.importers)})`);

    const later = Object.keys(stages).filter((name) => name !== "base");
    for (const name of later) {
        if (stages[name].pyodide.length) await py.loadPackage(stages[name].pyodide, { messageCallback: () => {} });
        installWheels(py, stages[name].wheels);
    }
    await py.runPythonAsync(AFTER_STAGES(later));
    out.checks.stages = { ms: ms(), loaded: later };

    const imports = JSON.parse(await py.runPythonAsync(IMPORTS));
    out.checks.imports = { ms: ms(), ...imports };
    for (const [module, error] of Object.entries(imports.failed)) fail("imports", `${module}: ${error}`);

    const staged = JSON.parse(await py.runPythonAsync(STAGE_MODULES));
    out.checks.stage_modules = { ms: ms(), ...staged };
    for (const [module, error] of Object.entries(staged.failed)) fail("stage_modules", `${module}: ${error}`);

    const discovered = JSON.parse(await py.runPythonAsync(DISCOVER));
    out.checks.discover = { ms: ms(), ...discovered };
    for (const plugin of PLUGINS) {
        if (!discovered.by_prefix[plugin]) fail("discover", `no ${plugin}.* capability registered`);
    }

    const pixels = JSON.parse(await py.runPythonAsync(PIXELS));
    out.checks.pixels = { ms: ms(), ...pixels };
    if (!pixels.size || Math.abs(pixels.size.value - 0.5) > 1e-9) {
        fail("pixels", `the calibrated fixture reports ${JSON.stringify(pixels.size)} (${pixels.said.join("; ") || "nothing logged"})`);
    }
    out.printed = printed.slice(-20);
} catch (error) {
    fail("run", String(error && error.stack ? error.stack : error).slice(-3000));
}
out.ms = ms();
console.log(JSON.stringify(out));
process.exitCode = out.failed.length ? 1 : 0;
