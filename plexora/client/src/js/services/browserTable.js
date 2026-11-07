/**
 * PlexoraTable -- the website's reader of a picked cell table.
 *
 * Plexora's Python reads a project's table with polars; in WebAssembly polars
 * has no threads and a large CSV does not parse at all. So when the worker
 * loads a picked CSV it asks the page for its columns (a "columns" question,
 * with the column kinds polars' own reading of the head gave:
 * plexora/browser/columns.py), and the page parses the file in
 * workers of its own (workers/csvWorker.js over node/csv.js) and hands the
 * columns back, transferred, for Python to build the frame polars would have
 * read.
 *
 * A file with no quote in its head is cut at line starts into one run per
 * worker; a run that meets a quote anyway (a quoted field may hold a line
 * break, so a cut could have landed inside one) has the file read again as
 * one run. Nothing is kept here once Python has the columns.
 *
 *   parse(file, kinds) -> a promise of {rows, columns, parts, ms}
 *   parsed()           -> the last tables handed to Python, with their times
 *
 * Classic script, `window.PlexoraTable`; needs PlexoraPython and PlexoraPicks.
 */
(function (global) {
    "use strict";

    const WORKER = "/client/src/js/workers/csvWorker.js";
    const MODULE = "/client/src/js/node/csv.js";
    const HEAD = 4 << 20;
    //: A run per this many bytes, at most WORKERS of them.
    const RUN_BYTES = 24 << 20;
    const WORKERS = Math.min(8, Math.max(2, (global.navigator.hardwareConcurrency || 4) - 2));

    function run(file, start, end, kinds) {
        return new Promise((resolve, reject) => {
            const worker = new Worker(WORKER, { type: "module" });
            worker.onmessage = ({ data }) => {
                worker.terminate();
                if (data.ok) resolve(data.result);
                else reject(new Error(data.error));
            };
            worker.onerror = (event) => {
                worker.terminate();
                reject(new Error(event.message || "the CSV worker failed"));
            };
            worker.postMessage({ id: 0, file, start, end, kinds });
        });
    }

    async function parse(file, kinds) {
        const began = performance.now();
        const csv = await import(MODULE);
        const head = new Uint8Array(await file.slice(0, Math.min(file.size, HEAD)).arrayBuffer());
        const start = csv.headerEnd(head);
        if (start < 0) return { rows: 0, columns: kinds.map(() => null), parts: 0, ms: 0 };
        const quoted = head.indexOf(34, start) >= 0;
        const wanted = quoted ? 1 : Math.min(WORKERS, Math.max(1, Math.ceil((file.size - start) / RUN_BYTES)));
        const cuts = [start];
        for (let i = 1; i < wanted; i += 1) {
            const at = start + Math.floor(((file.size - start) * i) / wanted);
            const probe = new Uint8Array(await file.slice(at, Math.min(file.size, at + (1 << 20))).arrayBuffer());
            const next = csv.nextLineStart(probe);
            if (next < 0) continue;
            const cut = at + next;
            if (cut > cuts[cuts.length - 1] && cut < file.size) cuts.push(cut);
        }
        cuts.push(file.size);
        let parts = await Promise.all(cuts.slice(0, -1).map((from, i) => run(file, from, cuts[i + 1], kinds)));
        if (parts.length > 1 && parts.some((part) => part.sawQuote)) {
            parts = [await run(file, start, file.size, kinds)];
        }
        const merged = parts.length === 1 ? parts[0] : csv.merge(parts);
        return { rows: merged.rows, columns: merged.columns, parts: parts.length,
                 ms: Math.round(performance.now() - began) };
    }

    //: The arrays of a parsed table, to transfer rather than copy.
    function transfersOf(table) {
        const out = [];
        for (const column of (table && table.columns) || []) {
            if (!column) continue;
            for (const array of [column.values, column.missing, column.codes]) if (array) out.push(array.buffer);
        }
        return out;
    }

    //: Python asks for a picked table's columns; it gets them, or null (it
    //: then reads the file with polars, as anywhere else).
    //: The tables parsed for Python, newest last: {key, rows, parts, ms,
    //: failed, at} (diagnostics).
    const parsed = [];

    async function answer({ key, kinds }) {
        let table = null;
        let failed = null;
        const asked = performance.now();
        try {
            const file = global.PlexoraPicks ? global.PlexoraPicks.fileFor(key) : null;
            if (!file) failed = "the page does not hold that file now";
            else table = await parse(file, kinds);
        } catch (error) {
            failed = String(error && error.message || error).slice(0, 500);
        }
        parsed.push({ key, rows: table ? table.rows : 0, parts: table ? table.parts : 0,
                      ms: table ? table.ms : null, failed, at: Math.round(asked) });
        if (parsed.length > 20) parsed.shift();
        if (failed) throw new Error(failed);
        return { value: table, transfer: transfersOf(table) };
    }

    if (global.PlexoraPython && global.PlexoraPython.answers) global.PlexoraPython.answers("columns", answer);

    global.PlexoraTable = Object.freeze({ parse, parsed: () => parsed.slice() });
})(window);
