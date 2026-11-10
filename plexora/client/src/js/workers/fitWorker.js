// A helper Python for the website's contrast fits (services/fitHelpers.js):
// Pyodide with numpy and scikit-learn only -- not Plexora, not Flask -- so the
// main Python's one thread hands a channel's mixture fit here instead of
// spending up to seconds on it while every request waits.
//
// The fit is plexora/server/models/mixture_fit.py exactly as the main Python
// runs it, sent with each fit: its compiled file (the bundle ships no
// sources; the same Pyodide reads it here) or, where Python has one, its text.
// The same numpy and scikit-learn builds from the same bundle, the same bytes
// in: its floats are the ones the main Python would have made, bit for bit.
//
// `?base=` is the Python bundle's folder (py/<version>/), as the main worker's.
//
// Messages from the page:
//   ready                            -> {op: "ready"} once loaded, or {op: "failed", error}
//   fit {id, values, components, code, codeKind, codeId}
//                                    values: the float64 values' bytes (a
//                                    channel's log intensities, a marker's cells);
//                                    code: mixture_fit's ("pyc" or "source");
//                                    -> {op: "done", id, params} (hex of 3 x
//                                    components float64s) or {op: "done", id, error}

const base = new URL(self.location.href).searchParams.get("base") || "";

let py = null;
let defined = null;

const loading = (async () => {
    const { loadPyodide } = await import(`${base}pyodide/pyodide.mjs`);
    py = await loadPyodide({ indexURL: `${base}pyodide/` });
    await py.loadPackage(["numpy", "scikit-learn"], { messageCallback: () => {} });
    // Imported now, not at the first fit: scikit-learn's import is most of a
    // second, which a helper pays once, before anyone waits on it.
    py.runPython("import numpy\nimport sklearn.mixture");
})();

function define(code, kind, id) {
    if (defined === id) return;
    py.globals.set("_code", code);
    py.globals.set("_kind", kind);
    py.runPython([
        "import marshal, sys, types",
        "_data = bytes(_code.to_py())",
        "_module = types.ModuleType('plexora_mixture_fit')",
        // A compiled file: its 16-byte header, then the module's code.
        "_compiled = marshal.loads(_data[16:]) if _kind == 'pyc' else compile(_data.decode('utf-8'), 'mixture_fit.py', 'exec')",
        "exec(_compiled, _module.__dict__)",
        "sys.modules['plexora_mixture_fit'] = _module",
        "del _code, _kind, _data, _module, _compiled",
    ].join("\n"));
    defined = id;
}

function fit(values, components) {
    py.globals.set("_values", values);
    py.globals.set("_components", components);
    try {
        return py.runPython([
            "import numpy as _np, sys as _sys",
            "_x = _np.frombuffer(bytes(_values.to_py()), dtype=_np.float64)",
            "_sys.modules['plexora_mixture_fit'].fit_mixture(_x, int(_components)).tobytes().hex()",
        ].join("\n"));
    } finally {
        py.runPython("del _values, _components");
    }
}

self.onmessage = async ({ data }) => {
    if (!data) return;
    if (data.op === "ready") {
        try {
            await loading;
            self.postMessage({ op: "ready" });
        } catch (error) {
            self.postMessage({ op: "failed", error: String(error && error.message || error).slice(0, 300) });
        }
        return;
    }
    if (data.op === "fit") {
        try {
            await loading;
            define(data.code, data.codeKind, data.codeId);
            self.postMessage({ op: "done", id: data.id, params: fit(data.values, data.components || 3) });
        } catch (error) {
            self.postMessage({ op: "done", id: data.id, error: String(error && error.message || error).slice(0, 300) });
        }
    }
};
