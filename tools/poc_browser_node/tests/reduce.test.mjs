// lib/reduce.js against Python's own arithmetic (tests/reduce_vectors.json,
// written by bench/reduce_vectors.py from image_pyramid.reduce2 and numpy's
// [::f, ::f]): every dtype a flat file can hold, odd and even sides, rounding
// ties, negatives and extremes. A pyramid built in the browser has to be
// Python's value for value, so equality here is exact.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { derive, factorsFor } from "../page/src/lib/reduce.js";

const vectors = JSON.parse(readFileSync(new URL("./reduce_vectors.json", import.meta.url), "utf8"));
const KINDS = { uint8: Uint8Array, uint16: Uint16Array, uint32: Uint32Array, int8: Int8Array,
                int16: Int16Array, int32: Int32Array, float32: Float32Array };

const flat = (rows) => rows.flat();

for (const c of vectors.cases) {
    test(`${c.dtype} ${c.shape.join("x")}`, () => {
        const Kind = KINDS[c.dtype];
        const [h, w] = c.shape;
        const block = Kind.from(flat(c.block));
        const factors = [1, ...c.factors];
        const image = derive(block, h, w, factors, "image");
        assert.equal(image.length, c.cascade.length);
        image.forEach((level, k) => {
            const want = Kind.from(flat(c.cascade[k]));
            assert.equal(level.height, c.cascade[k].length, `level ${k + 1} height`);
            assert.deepEqual(Array.from(level.array), Array.from(want), `mean level ${k + 1}`);
        });
        const labels = derive(block, h, w, factors, "labels");
        labels.forEach((level, k) => {
            assert.deepEqual(Array.from(level.array), Array.from(Kind.from(flat(c.nearest[k]))), `nearest level ${k + 1}`);
        });
    });
}

test("factorsFor is pyramid_factors", () => {
    assert.deepEqual(factorsFor(1024, 1024), [1]);
    assert.deepEqual(factorsFor(1025, 10), [1, 2]);
    assert.deepEqual(factorsFor(45381, 52923), [1, 2, 4, 8, 16, 32, 64]);
});
