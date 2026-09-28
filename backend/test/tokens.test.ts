import { describe, expect, it } from 'vitest';

import type { Env } from '../src/env';
import { percentile } from '../src/telemetry/hist';
import { base64url, bearer, mintToken, verifyToken } from '../src/telemetry/tokens';

const ID = 'ab'.repeat(16);
const env = (current: string, previous?: string) =>
  ({ TELEMETRY_HMAC_KEY: current, TELEMETRY_HMAC_KEY_PREVIOUS: previous }) as unknown as Env;

describe('install tokens', () => {
  it('round-trip, with the PLEXORAT1 format and payload', async () => {
    const { token, expires } = await mintToken(env('k1'), ID, 1000, 365 * 86400);
    expect(token.startsWith('PLEXORAT1.')).toBe(true);
    expect(expires).toBe(1000 + 365 * 86400);
    const verified = await verifyToken(env('k1'), token, 2000);
    expect(verified).toEqual({ payload: { v: 1, kind: 'telemetry', tid: ID, iat: 1000, exp: expires }, rotate: false });
  });

  it('refuses tampering, other keys, other prefixes and expiry', async () => {
    const { token } = await mintToken(env('k1'), ID, 1000, 100);
    const [prefix, body, sig] = token.split('.') as [string, string, string];
    expect(await verifyToken(env('k2'), token, 1050)).toBeNull();
    expect(await verifyToken(env('k1'), token, 1101)).toBeNull();
    expect(await verifyToken(env('k1'), `SCIMAPPROT1.${body}.${sig}`, 1050)).toBeNull();
    const forged = base64url(new TextEncoder().encode(JSON.stringify({ v: 1, kind: 'telemetry', tid: 'cd'.repeat(16), iat: 1000, exp: 9e9 })));
    expect(await verifyToken(env('k1'), `${prefix}.${forged}.${sig}`, 1050)).toBeNull();
    expect(await verifyToken(env('k1'), `${prefix}.${body}`, 1050)).toBeNull();
    expect(await verifyToken(env('k1'), `${prefix}.${body}.!!!`, 1050)).toBeNull();
  });

  it('rotation: the previous key still verifies, flagged rotate', async () => {
    const { token } = await mintToken(env('old'), ID, 1000, 1000);
    expect(await verifyToken(env('new', 'old'), token, 1500)).toMatchObject({ rotate: true });
    expect(await verifyToken(env('new'), token, 1500)).toBeNull();
  });

  it('bearer parsing', () => {
    expect(bearer('Bearer abc')).toBe('abc');
    expect(bearer('bearer  abc ')).toBe('abc');
    expect(bearer('Basic abc')).toBeNull();
    expect(bearer(undefined)).toBeNull();
  });
});

describe('histogram percentiles', () => {
  it('interpolate within the bin and cap the open top bin at max', () => {
    expect(percentile([0, 0, 0, 0, 0, 0, 0, 0, 0], 0.5)).toBeNull();
    expect(percentile([10, 0, 0, 0, 0, 0, 0, 0, 0], 0.5)).toBeCloseTo(8);
    expect(percentile([0, 10, 0, 0, 0, 0, 0, 0, 0], 1)).toBeCloseTo(50);
    expect(percentile([0, 0, 0, 0, 0, 0, 0, 0, 4], 1, undefined, 9000)).toBeCloseTo(9000);
    // Bands add: two halves give the same answer as the whole.
    const a = [1, 2, 3, 0, 0, 0, 0, 0, 0];
    const b = [3, 2, 1, 0, 0, 0, 0, 0, 0];
    expect(percentile(a.map((n, i) => n + b[i]!), 0.5)).toBeCloseTo(percentile([4, 4, 4, 0, 0, 0, 0, 0, 0], 0.5)!);
  });
});
