import { defineConfig } from 'vitest/config';

/**
 * Pure units in plain node: the vectors interpreter, the fixture contract,
 * tokens and the histogram maths. `vectors.test.ts` and `fixture.test.ts` read
 * files with `node:fs`, which the Workers pool cannot.
 */
export default defineConfig({
  test: {
    name: 'node',
    include: ['test/*.test.ts'],
  },
});
