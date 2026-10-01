import { readFileSync } from 'node:fs';

import { defineWorkersConfig } from '@cloudflare/vitest-pool-workers/config';

/**
 * Route tests: the real Worker in workerd against local D1 and R2, driven
 * through `SELF.fetch` and the exported `scheduled` handler. schema.sql is read
 * node-side and handed in as a binding, because nothing in a Worker can read
 * from disk; applying the real file is what makes a schema change break tests.
 *
 * The signing key is a THROWAWAY generated for the suite (test/routes/keys.ts):
 * no production key is ever needed, or trusted, by a test.
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
            // The public half of the test seed in test/routes/keys.ts.
            ACTIVE_KID: 'pxt',
            PUBLIC_KEYS_JSON: '',
            SIGNING_KEY_PXT: 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8',
            FP_PEPPER: 'test-fp-pepper',
            IP_HASH_KEY: 'test-ip-pepper',
            ADMIN_TOKEN: 'test-admin',
            SESSION_KEY: 'test-session-key',
            KEY_VAULT_KEY: 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=',
            PUBLIC_BASE_URL: 'http://localhost',
            // Provider keys only have to exist: tests replace the network (setUpstreamFetch).
            ANTHROPIC_API_KEY: 'test-anthropic', OPENAI_API_KEY: 'test-openai', OPENROUTER_API_KEY: 'test-openrouter',
            ORCAROUTER_API_KEY: 'test-orcarouter', SAYGM_API_KEY: 'test-saygm',
            AI_RETRY_BACKOFF_MS: '0',
          },
        },
      },
    },
  },
});
