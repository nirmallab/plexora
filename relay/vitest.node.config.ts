import { defineConfig } from 'vitest/config';

/**
 * Pure units in plain node: hosts, subprotocols, origins, and the certificate check against the `biocognia` package's
 * vectors and against licenseVerify.js's key table (both read from disk, which the Workers pool cannot).
 */
export default defineConfig({
  test: {
    name: 'node',
    include: ['test/*.test.ts'],
  },
});
