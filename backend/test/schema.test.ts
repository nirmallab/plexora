/** The generic interpreter, on hand-written specs and the real vectors. */
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import { cleanBatch, cleanClient, compile, tally } from '../src/telemetry/schema';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/sample-batch.json', import.meta.url), 'utf8'));
const clone = () => structuredClone(fixture) as Record<string, any>;
const ok = (value: unknown, spec: Parameters<typeof compile>[0]) => typeof compile(spec)(value, 'diagnostics') !== 'symbol';
/** The cleaned value and how many unknown values it lost; `v` is a symbol when invalid. */
const run = (value: unknown, spec: Parameters<typeof compile>[0], strict = false) => {
  const t = tally();
  return { v: compile(spec, undefined, strict)(value, 'diagnostics', t), unknown: t.unknown };
};
const invalid = (value: unknown, spec: Parameters<typeof compile>[0], strict = false) =>
  typeof run(value, spec, strict).v === 'symbol';

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
    expect(ok(['a', 'a', 'a'], list)).toBe(false); // over the cap is shape
    expect(ok('a', list)).toBe(false);
    const map = { t: 'map', k: { t: 'pattern', re: '[a-z]+', max: 8 }, v: { t: 'int', max: 9 }, max: 2 } as never;
    expect(ok({ a: 1 }, map)).toBe(true);
    expect(ok({ a: 1, b: 2, c: 3 }, map)).toBe(false);
    const struct = { t: 'struct', f: { a: { t: 'int', max: 1 } } };
    expect(ok({}, struct)).toBe(true);
    expect(ok({ a: 1 }, struct)).toBe(true);
    expect(ok([], struct)).toBe(false);
  });

  it('drops and counts what the vectors do not know, instead of refusing the value', () => {
    const list = { t: 'list', of: { t: 'enum', v: ['a'] }, max: 4 };
    expect(run(['a', 'b', 'a'], list)).toEqual({ v: ['a', 'a'], unknown: 1 });
    expect(run(['b'], list)).toEqual({ v: [], unknown: 1 });
    const map = { t: 'map', k: { t: 'pattern', re: '[a-z]+', max: 8 }, v: { t: 'int', max: 9 }, max: 4 } as never;
    expect(run({ a: 1, A: 1, b: 10 }, map)).toEqual({ v: { a: 1 }, unknown: 2 });
    const struct = { t: 'struct', f: { a: { t: 'int', max: 1 } } };
    expect(run({ a: 1, b: 1 }, struct)).toEqual({ v: { a: 1 }, unknown: 1 });
    expect(run({ a: 2 }, struct)).toEqual({ v: {}, unknown: 1 });
    // Strict, as list items, map values and counter dims are: any fault is INVALID.
    expect(invalid(['b'], list, true)).toBe(true);
    expect(invalid({ A: 1 }, map, true)).toBe(true);
    expect(invalid({ b: 1 }, struct, true)).toBe(true);
  });

  it('a struct inside a list is atomic: one unknown field drops the whole item', () => {
    const plugins = {
      t: 'list', max: 8,
      of: { t: 'struct', f: { id: { t: 'enum', v: ['gating'] }, version: { t: 'pattern', re: '[0-9.]+', max: 8 } } },
    };
    expect(run([{ id: 'gating', version: '1.0' }, { id: 'new_plugin', version: '1.0' }], plugins))
      .toEqual({ v: [{ id: 'gating', version: '1.0' }], unknown: 1 });
    expect(run([{ id: 'gating', extra: 1 }], plugins)).toEqual({ v: [], unknown: 1 });
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
    const pruned = cleanBatch(batch);
    if (!pruned.ok) throw new Error(pruned.error);
    expect('license_id_hash' in pruned.batch.client).toBe(false);
    expect(pruned.batch.unknown).toBe(1);
    expect(JSON.stringify(pruned.batch)).not.toContain('not hex');
  });

  it('the fixture loses nothing to the allowlist', () => {
    const result = cleanBatch(clone());
    expect(result.ok && result.batch.unknown).toBe(0);
  });

  it('still rejects a malformed event whole, and keeps the rest', () => {
    const cases: ((b: Record<string, any>) => void)[] = [
      (b) => { delete b.events[0].window; },
      (b) => { b.events[0].window = '2026-09-27'; },
      (b) => { b.events[0].extra = 1; },
      (b) => { b.events[0].props = []; },
      (b) => { b.events[5].rows[6].h = [1, 2, 3, 4, 5, 6, 7, 8, 9]; }, // count key with a histogram
      (b) => { b.events[5].rows[0].h = [1, 2, 3]; },
      (b) => { b.events[5].rows[0].h = [1, 2, 3]; b.events[5].rows[0].d.route = 'new_route'; }, // shape first
      (b) => { b.events[5].rows[0].d = 'x'; },
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
      expect(result.batch.unknown, mutate.toString()).toBe(0);
      expect(result.batch.events).toHaveLength(fixture.events.length - 1);
    }
  });

  it('prunes what a newer client says that the vectors do not know, and keeps the event', () => {
    const cases: [string, (b: Record<string, any>) => void, (r: Record<string, any>) => void][] = [
      ['an invalid record prop', (b) => { b.events[0].props.cpus = '3'; },
        (r) => expect(r.events[0].props.cpus).toBeUndefined()],
      ['an unknown record prop', (b) => { b.events[0].props.new_prop = 1; },
        (r) => expect(r.events[0].props.new_prop).toBeUndefined()],
      ['an unknown plugin in a list', (b) => { b.events[0].props.plugins_installed = ['gating', 'new_plugin']; },
        (r) => expect(r.events[0].props.plugins_installed).toEqual(['gating'])],
      ['an unknown dim value', (b) => { b.events[5].rows[0].d.route = '/api/secret'; },
        (r) => expect(r.events[5].rows).toHaveLength(8)],
      ['an unknown dim name', (b) => { b.events[5].rows[0].d.new_dim = 'x'; },
        (r) => expect(r.events[5].rows).toHaveLength(8)],
      ['an unknown counter key', (b) => { b.events[5].rows[0].k = 'unknown'; },
        (r) => expect(r.events[5].rows).toHaveLength(8)],
      ['an unknown tool on a tool.summary row', (b) => { b.events[11].rows[0].d.tool = 'new_tool'; },
        (r) => expect(r.events[11].rows).toHaveLength(5)],
      ['an unknown plugin id in the client block', (b) => { b.client.plugins[0].id = 'new_plugin'; },
        (r) => expect(r.client.plugins).toEqual([])],
      ['an unknown client key', (b) => { b.client.hostname = 'lab-mac'; },
        (r) => expect(r.client.hostname).toBeUndefined()],
    ];
    for (const [label, mutate, verify] of cases) {
      const batch = clone();
      mutate(batch);
      const result = cleanBatch(batch);
      if (!result.ok) throw new Error(`${label}: ${result.error}`);
      expect(result.batch.rejected, label).toBe(0);
      expect(result.batch.dropped, label).toBe(0);
      expect(result.batch.unknown, label).toBe(1);
      expect(result.batch.events, label).toHaveLength(fixture.events.length);
      verify(result.batch);
      const text = JSON.stringify(result.batch);
      for (const word of ['secret', 'new_plugin', 'new_prop', 'new_dim', 'new_tool', 'lab-mac', '"k":"unknown"']) {
        expect(text, label).not.toContain(word);
      }
    }
  });

  it('an unknown row in an event that is rejected anyway is not counted as unknown', () => {
    const batch = clone();
    batch.events[5].rows[0].d.route = 'new_route';
    batch.events[5].rows[1].n = -1;
    const result = cleanBatch(batch);
    if (!result.ok) throw new Error(result.error);
    expect(result.batch).toMatchObject({ rejected: 1, unknown: 0 });
  });

  it('a counter event whose only row is unknown is dropped, not rejected', () => {
    const batch = clone();
    batch.events[11].rows = [{ ...batch.events[11].rows[0], d: { tool: 'new_tool' } }];
    const result = cleanBatch(batch);
    if (!result.ok) throw new Error(result.error);
    expect(result.batch).toMatchObject({ rejected: 0, dropped: 1, unknown: 1 });
    expect(result.batch.events.map((e) => e.type)).not.toContain('tool.summary');
  });

  it('a client mode it does not know is dropped, which means anonymous', () => {
    const batch = clone();
    batch.client.mode = 'full';
    const result = cleanBatch(batch);
    if (!result.ok) throw new Error(result.error);
    expect(result.batch.client.mode).toBeUndefined();
    expect(result.batch.unknown).toBe(1);
    const opened = result.batch.events.find((e) => e.type === 'dataset.opened')!;
    expect(opened.props!.channels).toBeUndefined(); // a diagnostics field, stripped
  });

  it('cleanClient keeps a newer client block and counts what it lost', () => {
    const t = tally();
    const client = cleanClient({ plexora_version: '0.0.31', hostname: 'lab-mac', plugins: [{ id: 'new_plugin' }] }, t);
    expect(client).toEqual({ plexora_version: '0.0.31', plugins: [] });
    expect(t.unknown).toBe(2);
    expect(cleanClient('x')).toBeNull();
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
