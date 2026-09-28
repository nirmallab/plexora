/**
 * Applies the real schema.sql to the isolate's local D1 before each test file,
 * and resets module-level state before each test. The Worker cannot read from
 * disk, so the file arrives as a binding (vitest.workers.config.ts).
 */
import { env } from 'cloudflare:test';
import { beforeEach } from 'vitest';

import { resetAccessCache } from '../../src/access';
import { resetOutbox } from '../../src/email';
import { setClockOffset } from '../../src/env';

export function statementsIn(sql: string): string[] {
  return sql
    .split('\n')
    .filter((line) => !line.trim().startsWith('--'))
    .join('\n')
    .split(';')
    .map((statement) => statement.trim())
    .filter(Boolean);
}

await env.LICENSE_DB.batch(statementsIn(env.TEST_SCHEMA_SQL).map((statement) => env.LICENSE_DB.prepare(statement)));

beforeEach(() => {
  resetOutbox();
  resetAccessCache();
  setClockOffset(0);
});
