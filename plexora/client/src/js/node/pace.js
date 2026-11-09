/**
 * At most `limit` pieces of work under way at once, the rest waiting their
 * turn in the order they came; the limit can change while work runs (a
 * raised one starts waiting work at once, a lowered one lets what runs end).
 *
 * The website's pyramid builds run through one (workers/pyramidWorker.js):
 * on every hand while nothing else is under way, on one while the page waits
 * on something (services/browserPyramid.js `pace`).
 *
 *   const pace = pacer();
 *   pace.run(() => work())  -> work()'s promise, once its turn came
 *   pace.set(n)             at most n at once from now on (n >= 1)
 *   pace.active             how many run now
 */
export function pacer(limit = Infinity) {
    let active = 0;
    const queued = [];

    function drain() {
        while (queued.length && active < limit) {
            active += 1;
            queued.shift()();
        }
    }

    function run(work) {
        let turn;
        if (active < limit) {
            active += 1;
            turn = Promise.resolve();
        } else {
            turn = new Promise((resolve) => queued.push(resolve));
        }
        return turn.then(work).finally(() => {
            active -= 1;
            drain();
        });
    }

    function set(n) {
        limit = Math.max(1, Number(n) || 1);
        drain();
    }

    return {
        run,
        set,
        get active() { return active; },
    };
}
