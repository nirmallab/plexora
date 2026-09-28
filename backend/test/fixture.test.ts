/**
 * The cross-language contract: the batch the Python client generates
 * (`plexora telemetry sample --fixture`) must clean with nothing rejected.
 */
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import { cleanBatch } from '../src/telemetry/schema';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/sample-batch.json', import.meta.url), 'utf8'));

describe('test/fixtures/sample-batch.json', () => {
  it('passes cleanBatch with rejected == 0 and nothing dropped', () => {
    const result = cleanBatch(fixture);
    if (!result.ok) throw new Error(result.error);
    expect(result.batch.rejected).toBe(0);
    expect(result.batch.dropped).toBe(0);
    expect(result.batch.events).toHaveLength(fixture.events.length);
  });

  it('survives cleaning unchanged in diagnostics mode (nothing silently trimmed)', () => {
    const result = cleanBatch(fixture);
    if (!result.ok) throw new Error(result.error);
    expect(JSON.parse(JSON.stringify(result.batch.events))).toEqual(fixture.events);
    expect(result.batch.client).toEqual(fixture.client);
  });

  it('covers every event type in the vectors', async () => {
    const vectors = JSON.parse(readFileSync(new URL('../vectors/plexora-vectors.json', import.meta.url), 'utf8'));
    const types = new Set(fixture.events.map((e: { type: string }) => e.type));
    for (const type of Object.keys(vectors.events)) expect(types.has(type), type).toBe(true);
  });
});
