import type { Env } from '../../src/env';

declare module 'cloudflare:test' {
  interface ProvidedEnv extends Env {
    /** schema.sql, read node-side in vitest.workers.config.ts. */
    TEST_SCHEMA_SQL: string;
  }
}
