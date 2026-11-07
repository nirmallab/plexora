/**
 * One run of a picked CSV's records, parsed by node/csv.js (a module worker;
 * services/browserTable.js runs several over a file).
 *
 * {id, file, start, end, kinds} -> {id, ok, result | error}
 *
 * [start, end) begins at a record and ends at one (or at the file's end). It
 * is read 8 MB at a time; a record cut by a piece waits for the next one. The
 * result's arrays are transferred, never copied; `sawQuote` says the run held
 * a quote, which the page needs to know when it cut the file at line starts.
 */
import { Columns } from "../node/csv.js";

const STEP = 8 * 1024 * 1024;

self.onmessage = async ({ data }) => {
    const { id, file, start, end, kinds } = data;
    try {
        const columns = new Columns(kinds);
        let carry = new Uint8Array(0);
        let at = start;
        for (;;) {
            const take = Math.min(STEP, end - at);
            const fresh = take > 0 ? new Uint8Array(await file.slice(at, at + take).arrayBuffer())
                : new Uint8Array(0);
            at += take;
            let bytes = fresh;
            if (carry.length) {
                bytes = new Uint8Array(carry.length + fresh.length);
                bytes.set(carry);
                bytes.set(fresh, carry.length);
            }
            const final = at >= end;
            const took = columns.add(bytes, final);
            if (final) break;
            carry = bytes.slice(took);
        }
        const result = columns.result();
        const transfer = [];
        for (const column of result.columns) {
            if (!column) continue;
            for (const array of [column.values, column.missing, column.codes]) if (array) transfer.push(array.buffer);
        }
        self.postMessage({ id, ok: true, result }, transfer);
    } catch (error) {
        self.postMessage({ id, ok: false, error: String(error && error.stack || error) });
    }
};
