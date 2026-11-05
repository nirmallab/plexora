// node --test tests/ -- the page's numeric kernels against numpy's rules.
import { test } from "node:test";
import assert from "node:assert/strict";
import { histogram, percentile, percentiles, overviewOf } from "../page/src/stats.js";

function rng(seed) {
    let s = seed >>> 0;
    return () => {
        s = (s + 0x6d2b79f5) >>> 0;
        let t = Math.imul(s ^ (s >>> 15), 1 | s);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

test("quickselect percentiles equal sorted percentiles, ties and all", () => {
    const rand = rng(3);
    for (let trial = 0; trial < 60; trial += 1) {
        const n = 1 + Math.floor(rand() * 5000);
        const distinct = 1 + Math.floor(rand() * 50);   // many ties
        const values = Float64Array.from({ length: n }, () => Math.floor(rand() * distinct) - distinct / 2 + (trial % 2 ? rand() : 0));
        const sorted = Float64Array.from(values).sort();
        const want = [25, 50, 75].map((q) => percentile(sorted, q));
        const got = percentiles(Float64Array.from(values), [25, 50, 75]);
        assert.deepEqual(got, want, `trial ${trial} n=${n}`);
    }
});

test("quickselect is right past the Floyd-Rivest sampling size", () => {
    // The 2026-11-05 bug: each selection started AT the previous k, whose
    // first swap could move the value already selected there.
    const rand = rng(7);
    const values = Float64Array.from({ length: 200000 }, () => (rand() - 0.5) * Math.PI);
    const sorted = Float64Array.from(values).sort();
    assert.deepEqual(percentiles(Float64Array.from(values), [25, 50, 75]),
                     [25, 50, 75].map((q) => percentile(sorted, q)));
});

test("histogram follows numpy: closed last bin, density", () => {
    // np.histogram(np.arange(11), bins=5, density=True)
    const h = histogram(Float64Array.from({ length: 11 }, (_, i) => i), 5);
    assert.deepEqual(h.map((b) => b.x), [1, 3, 5, 7, 9]);
    const counts = h.map((b) => Math.round(b.y * 2 * 11));
    assert.deepEqual(counts, [2, 2, 2, 2, 3]);
});

test("overview pools toward 200 with zero padding, as skimage block_reduce", () => {
    // 450 x 450 -> reduce = 2 -> 225 x 225; an odd edge averages with zeros.
    const w = 451, h = 450;
    const values = new Float64Array(w * h).fill(4);
    const ov = overviewOf(values, w, h);
    assert.equal(ov.width, 226);
    assert.equal(ov.height, 225);
    assert.equal(ov.data[0], 4);
    assert.equal(ov.data[225], 2);   // last column: one real pixel of two, zero-padded
});
