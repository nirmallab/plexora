// How far a WebAssembly memory grows in a worker here (the self-check page,
// services/selfcheck.js): Python's heap in the website's worker is one such
// memory and cannot outgrow it. Grown 64 MiB at a time up to wasm32's 4 GiB,
// saying how far it got at every 256 MiB (a browser may end the page rather
// than refuse: the page keeps the last); the worker is ended at once after,
// which frees it.
//
//   {} -> {reached}..., then {bytes, error}
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
            if (pages % (4 * STEP) === 0) self.postMessage({ reached: pages * PAGE });
        } catch (failed) {
            error = String(failed && failed.message || failed);
            break;
        }
    }
    self.postMessage({ bytes: pages * PAGE, error });
};
