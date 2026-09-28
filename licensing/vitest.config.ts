import { defineConfig } from 'vitest/config';

/**
 * Pure units in plain node: canonical JSON and the cross-language vectors,
 * certificate signing and verification, clamps. `vectors.test.ts` reads files
 * with `node:fs`, which the Workers pool cannot.
 */
export default defineConfig({
  test: {
    name: 'node',
    include: ['test/*.test.ts'],
  },
});
