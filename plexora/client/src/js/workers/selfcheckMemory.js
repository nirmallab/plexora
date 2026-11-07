// How far a WebAssembly memory grows in a worker here (the self-check page,
// services/selfcheck.js): Python's heap in the website's worker is one such
// memory and cannot outgrow it. Grown 64 MiB at a time up to wasm32's 4 GiB;
// the worker is ended at once after, which frees it.
//
//   {} -> {bytes, error}
self.onmessage = () => {
    const PAGE = 65536;
    const MAX = 65536;
    const STEP = 1024;
    let memory = null;
    let pages = 0;
    let error = null;
    try {
        memory = new WebAssembly.Memory({ initial: 0, maximum: MAX });
    } catch (failed) {
        self.postMessage({ bytes: 0, error: String(failed && failed.message || failed) });
        return;
    }
    while (pages < MAX) {
        const more = Math.min(STEP, MAX - pages);
        try {
            memory.grow(more);
            pages += more;
        } catch (failed) {
            error = String(failed && failed.message || failed);
            break;
        }
    }
    self.postMessage({ bytes: pages * PAGE, error });
};
