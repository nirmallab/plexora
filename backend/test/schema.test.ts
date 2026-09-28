/** The generic interpreter, on hand-written specs and the real vectors. */
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import { cleanBatch, compile } from '../src/telemetry/schema';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/sample-batch.json', import.meta.url), 'utf8'));
const clone = () => structuredClone(fixture) as Record<string, any>;
const ok = (value: unknown, spec: Parameters<typeof compile>[0]) => typeof compile(spec)(value, 'diagnostics') !== 'symbol';

describe('type tags', () => {
  it('enum', () => {
    expect(ok('a', { t: 'enum', v: ['a', 'b'] })).toBe(true);
    expect(ok('c', { t: 'enum', v: ['a', 'b'] })).toBe(false);
    expect(ok(1, { t: 'enum', v: ['1'] })).toBe(false);
  });

  it('pattern is anchored and length-capped', () => {
    const spec = { t: 'pattern', re: '[a-z]+|x', max: 5 };
    expect(ok('abc', spec)).toBe(true);
    expect(ok('abc1', spec)).toBe(false);
    expect(ok('1x', spec)).toBe(false);
    expect(ok('abcdef', spec)).toBe(false);
    expect(ok('', { t: 'pattern', re: '.*', max: 5 })).toBe(false);
  });

  it('int is a non-negative safe integer up to max, never a bool', () => {
    expect(ok(3, { t: 'int', max: 3 })).toBe(true);
    expect(ok(4, { t: 'int', max: 3 })).toBe(false);
    expect(ok(-1, { t: 'int', max: 3 })).toBe(false);
    expect(ok(1.5, { t: 'int', max: 3 })).toBe(false);
    expect(ok(true, { t: 'int', max: 3 })).toBe(false);
  });

  it('bool, hist, list, map, struct', () => {
    expect(ok(false, { t: 'bool' })).toBe(true);
    expect(ok(0, { t: 'bool' })).toBe(false);
    expect(ok([0, 1, 2, 3, 4, 5, 6, 7, 8], { t: 'hist' })).toBe(true);
    expect(ok([0, 1, 2], { t: 'hist' })).toBe(false);
    expect(ok([0, 1, 2, 3, 4, 5, 6, 7, -8], { t: 'hist' })).toBe(false);
    const list = { t: 'list', of: { t: 'enum', v: ['a'] }, max: 2 };
    expect(ok(['a', 'a'], list)).toBe(true);
    expect(ok(['a', 'a', 'a'], list)).toBe(false);
    expect(ok(['b'], list)).toBe(false);
    const map = { t: 'map', k: { t: 'pattern', re: '[a-z]+', max: 8 }, v: { t: 'int', max: 9 }, max: 2 } as never;
    expect(ok({ a: 1 }, map)).toBe(true);
    expect(ok({ A: 1 }, map)).toBe(false);
    expect(ok({ a: 1, b: 2, c: 3 }, map)).toBe(false);
    const struct = { t: 'struct', f: { a: { t: 'int', max: 1 } } };
    expect(ok({}, struct)).toBe(true);
    expect(ok({ a: 1 }, struct)).toBe(true);
    expect(ok({ b: 1 }, struct)).toBe(false);
  });

  it('refuses an unknown tag at compile time', () => {
    expect(() => compile({ t: 'float' })).toThrow();
  });
});

describe('cleanBatch', () => {
  it('rejects bad envelopes as a whole', () => {
    expect(cleanBatch(null).ok).toBe(false);
    expect(cleanBatch({ ...clone(), schema: 2 }).ok).toBe(false);
    expect(cleanBatch({ ...clone(), batch_id: 'ABABABABABABABABABABABABABABABAB' }).ok).toBe(false);
    expect(cleanBatch({ ...clone(), extra: 1 }).ok).toBe(false);
    const noId = clone();
    delete noId.client.install_id;
    expect(cleanBatch(noId).ok).toBe(false);
    const tooMany = clone();
    tooMany.events = Array.from({ length: 201 }, () => fixture.events[0]);
    expect(cleanBatch(tooMany).ok).toBe(false);
  });

  it('accepts reserved licence fields when present', () => {
    const batch = clone();
    batch.client.license_id_hash = 'ab'.repeat(16);
    const result = cleanBatch(batch);
    expect(result.ok && result.batch.client.license_id_hash).toBe('ab'.repeat(16));
    batch.client.license_id_hash = 'not hex';
    expect(cleanBatch(batch).ok).toBe(false);
  });

  it('rejects, never trims, a bad event, and keeps the rest', () => {
    const cases: ((b: Record<string, any>) => void)[] = [
      (b) => { delete b.events[0].window; },
      (b) => { b.events[0].window = '2026-09-27'; },
      (b) => { b.events[0].extra = 1; },
      (b) => { b.events[0].props.cpus = '3'; },
      (b) => { b.events[5].rows[0].d.route = '/api/secret'; },
      (b) => { b.events[5].rows[6].h = [1, 2, 3, 4, 5, 6, 7, 8, 9]; }, // count key with a histogram
      (b) => { b.events[5].rows[0].h = [1, 2, 3]; },
      (b) => { b.events[5].rows[0].k = 'unknown'; },
      (b) => { b.events[5].rows[0].n = -1; },
      (b) => { b.events[5].rows[0].s = 'x'; },
      (b) => { b.events[5].rows[0].extra = 1; },
      (b) => { b.events[5].rows = []; },
      (b) => { b.events[5].rows = Array.from({ length: 501 }, () => b.events[5].rows[6]); },
      (b) => { b.events[5].d = { route: 'x' }; }, // no event-level dims
      (b) => { b.events[0].type = 'session.start'; },
    ];
    for (const mutate of cases) {
      const batch = clone();
      mutate(batch);
      const result = cleanBatch(batch);
      if (!result.ok) throw new Error(result.error);
      expect(result.batch.rejected, mutate.toString()).toBe(1);
      expect(result.batch.events).toHaveLength(fixture.events.length - 1);
    }
  });

  it('strips diagnostics fields at the anonymous level and drops by priority', () => {
    const result = cleanBatch(clone(), { levelMax: 'anonymous', maxPriority: 3 });
    if (!result.ok) throw new Error(result.error);
    expect(result.batch.events.map((e) => e.type).sort()).toEqual(['dataset.opened', 'error.fingerprint', 'project.load', 'session.summary']);
    const opened = result.batch.events.find((e) => e.type === 'dataset.opened')!;
    expect(opened.props!.channels).toBeUndefined();
    expect(result.batch.dropped).toBe(fixture.events.length - 4);
  });
});
