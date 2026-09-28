/**
 * The vectors file is generated from the Python client's schema.py
 * (scripts/sync_telemetry_vectors.py). These tests pin that it parses, that
 * every type tag in it is one the interpreter supports, and that it compiles.
 */
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import { compileVectors, SUPPORTED_TAGS, type Vectors } from '../src/telemetry/schema';

const vectors = JSON.parse(
  readFileSync(new URL('../vectors/plexora-vectors.json', import.meta.url), 'utf8'),
) as Vectors;

function tagsIn(value: unknown, out = new Set<string>()): Set<string> {
  if (Array.isArray(value)) value.forEach((item) => tagsIn(item, out));
  else if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if (typeof record.t === 'string') out.add(record.t);
    Object.values(record).forEach((item) => tagsIn(item, out));
  }
  return out;
}

describe('plexora-vectors.json', () => {
  it('parses and has the envelope the interpreter reads', () => {
    expect(vectors.schema).toBe(1);
    expect(vectors.hist_bins).toBe(9);
    expect(typeof vectors.window_pattern).toBe('string');
    expect(vectors.caps).toMatchObject({ max_events: expect.any(Number), max_rows: expect.any(Number) });
    expect(Object.keys(vectors.events).length).toBeGreaterThan(0);
    expect(vectors.client.install_id).toBeDefined();
  });

  it('uses only supported type tags', () => {
    const supported = new Set<string>(SUPPORTED_TAGS);
    for (const tag of tagsIn(vectors)) expect(supported.has(tag), `unsupported tag ${tag}`).toBe(true);
  });

  it('every field, key and dim carries a mode, and every event a kind and priority', () => {
    const modes = new Set(['anonymous', 'diagnostics']);
    for (const spec of Object.values(vectors.client)) expect(modes.has(spec.m!)).toBe(true);
    for (const [type, event] of Object.entries(vectors.events)) {
      expect(['record', 'counter'], type).toContain(event.kind);
      expect(Number.isInteger(event.priority), type).toBe(true);
      for (const prop of Object.values(event.props ?? {})) expect(modes.has(prop.m!), type).toBe(true);
      for (const [name, key] of Object.entries(event.keys ?? {})) {
        expect(['count', 'hist'], `${type}.${name}`).toContain(key.agg);
        expect(modes.has(key.m), `${type}.${name}`).toBe(true);
        for (const dim of Object.values(key.dims)) expect(modes.has(dim.m!), `${type}.${name}`).toBe(true);
      }
    }
  });

  it('compiles, including every regex', () => {
    expect(() => compileVectors(vectors)).not.toThrow();
  });

  it('allowlists the reserved licence fields in the client block', () => {
    for (const field of vectors.reserved_license_fields) expect(vectors.client[field], field).toBeDefined();
  });
});
