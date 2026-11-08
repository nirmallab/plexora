import { defineConfig } from 'vitest/config';

/**
 * End to end through a real `wrangler dev --local` (no Cloudflare account involved), with Node's own WebSocket client:
 * the real HTTP upgrade, subprotocol answer, Origin header and close codes a browser sees.
 */
export default defineConfig({
  test: {
    name: 'dev',
    include: ['test/dev/**/*.test.ts'],
    testTimeout: 30_000,
    hookTimeout: 90_000,
  },
});
