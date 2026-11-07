/**
 * PlexoraOnnx -- magic select's model on the website.
 *
 * On the desktop, Python runs MobileSAM on ONNX Runtime. On the website
 * Python runs in Pyodide, which has no ONNX Runtime, so the page runs the same
 * two model files on onnxruntime-web -- on the GPU through WebGPU where the
 * browser has an adapter, on WebAssembly threads otherwise -- in a worker of
 * its own (workers/onnxWorker.js), and Python asks it, through the worker's
 * ask channel, for exactly the two runs (plexora/browser/sam_page.py). Python
 * keeps everything else magic select does, so a click outlines the same
 * region here as there.
 *
 * Python's questions are of kind "sam": `{op: "status" | "install" | "cancel"
 * | "load" | "embed" | "decode" | "remove", ...}`, with the model's pins
 * (vision/sam_weights.py) where files are involved. The model is fetched
 * once, checked against its sha256, and kept in the browser's storage (OPFS
 * plexora/cache/models/); `install` starts that and `status` shows its
 * progress, so the viewer's one-time setup (services/segmentService.js) is
 * the desktop's, unchanged.
 *
 *   configure({ort, models, device})  where onnxruntime-web is served (the
 *                         Python bundle's ort/), where the model files are
 *                         (the assets' /models/), and a device to force:
 *                         "webgpu" | "wasm" (`?segment_device=` on the
 *                         address does the same)
 *   call(op, args)        the worker directly (diagnostics, tests)
 *   reset({device})       sessions dropped; the next run on `device`
 *   times()               the latest runs: [{op, ep, ms}]
 *
 * Classic script, `window.PlexoraOnnx`; registers with PlexoraPython.
 */
(function (global) {
    "use strict";

    const WORKER = "/client/src/js/workers/onnxWorker.js";
    const config = { ort: null, models: null, device: null };
    let worker = null;
    let nextId = 1;
    const waiting = new Map();

    try {
        const forced = new URLSearchParams(global.location.search).get("segment_device");
        if (forced) config.device = forced;
    } catch (error) { /* no address to read */ }

    //: The arrays of a message ({dtype, shape, data} values), moved rather
    //: than copied: whoever sent them is done with them.
    function transfersOf(message) {
        const out = [];
        for (const value of Object.values(message || {})) {
            const data = value && value.data;
            if (ArrayBuffer.isView(data) && data.buffer instanceof ArrayBuffer && !out.includes(data.buffer)) {
                out.push(data.buffer);
            }
        }
        return out;
    }

    function start() {
        if (worker) return worker;
        worker = new Worker(WORKER, { type: "module" });
        worker.onmessage = ({ data }) => {
            const pending = waiting.get(data.id);
            if (!pending) return;
            waiting.delete(data.id);
            if (data.ok) pending.resolve(data.result);
            else pending.reject(new Error(data.error));
        };
        worker.onerror = (event) => {
            const error = new Error(event.message || "the model's worker failed");
            for (const pending of waiting.values()) pending.reject(error);
            waiting.clear();
            worker = null;
        };
        worker.postMessage({ id: 0, op: "configure", args: { ...config } });
        return worker;
    }

    function call(op, args = {}, transfer = []) {
        const target = start();
        const id = nextId++;
        return new Promise((resolve, reject) => {
            waiting.set(id, { resolve, reject });
            target.postMessage({ id, op, args }, transfer);
        });
    }

    //: Python's "sam" questions. A failure is answered, not thrown: Python
    //: reads `{error}` and says it in the request's answer.
    async function answer(args) {
        try {
            const result = await call(args.op, args, transfersOf(args));
            return { value: result, transfer: transfersOf(result) };
        } catch (error) {
            return { value: { error: String((error && error.message) || error).slice(0, 300) } };
        }
    }

    function configure(next = {}) {
        for (const key of Object.keys(config)) if (next[key] !== undefined) config[key] = next[key];
        if (worker) worker.postMessage({ id: 0, op: "configure", args: { ...config } });
        return { ...config };
    }

    if (global.PlexoraPython && global.PlexoraPython.answers) global.PlexoraPython.answers("sam", answer);

    global.PlexoraOnnx = Object.freeze({
        configure,
        call: (op, args) => call(op, args || {}),
        reset: (args) => call("reset", args || {}),
        times: () => call("times"),
    });
})(window);
