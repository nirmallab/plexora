import { readFileSync } from 'node:fs';

import { defineWorkersConfig } from '@cloudflare/vitest-pool-workers/config';

/**
 * Route tests: the real Worker in workerd against local D1 and R2, driven
 * through `SELF.fetch` and the exported `scheduled` handler. schema.sql is read
 * node-side and handed in as a binding, because nothing in a Worker can read
 * from disk; applying the real file is what makes a schema change break tests.
 */
const schema = readFileSync(new URL('./schema.sql', import.meta.url), 'utf8');

export default defineWorkersConfig({
  esbuild: { jsx: 'automatic', jsxImportSource: 'hono/jsx' },
  test: {
    name: 'workers',
    include: ['test/routes/**/*.test.ts'],
    setupFiles: ['./test/routes/setup.ts'],
    poolOptions: {
      workers: {
        wrangler: { configPath: './wrangler.toml' },
        miniflare: {
          bindings: {
            TEST_SCHEMA_SQL: schema,
            TELEMETRY_HMAC_KEY: 'test-hmac-current',
            IP_HASH_KEY: 'test-ip-pepper',
            ADMIN_TOKEN: 'test-admin',
            // http, so the cookie helpers drop `Secure` and the tests can
            // follow the sign-in flow the way `wrangler dev` would.
            PUBLIC_BASE_URL: 'http://localhost',
          },
        },
      },
    },
  },
});
