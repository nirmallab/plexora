/**
 * The cross-language contract, from the Worker's side: re-sign every vector
 * payload with the vector seed and get the SAME bytes the Python tool wrote,
 * then check every certificate as the service would.
 */
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import { canonicalJson, checkCertificate, publicKeyOfSeed, signPayload } from '../src/certs';
import { base64url, base64urlDecode } from '../src/crypto';
import { PAID_ENTITLEMENTS } from '../src/billing';
import type { Env } from '../src/env';

const vectors = JSON.parse(readFileSync(new URL('../vectors/plexora-license-vectors.json', import.meta.url), 'utf8'));

function envFor(): Env {
  return { SIGNING_KEY_VX1: vectors.seed, ACTIVE_KID: 'vx1' } as unknown as Env;
}

describe('certificate vectors', () => {
  it('derives the published public key from the seed', async () => {
    expect(await publicKeyOfSeed(base64urlDecode(vectors.seed))).toBe(vectors.public_key);
    expect(await publicKeyOfSeed(base64urlDecode(vectors.delegation_seed))).toBe(vectors.delegation_public_key);
  });

  for (const vector of vectors.certificates) {
    it(`canonical JSON: ${vector.name}`, () => {
      expect(canonicalJson(vector.payload)).toBe(vector.canonical);
    });

    if (vector.valid) {
      it(`re-signs byte-for-byte: ${vector.name}`, async () => {
        const { body, signature } = await signPayload(base64urlDecode(vectors.seed), vector.payload);
        expect(`PLEXORA1.${vector.payload.kid}.${base64url(body)}.${base64url(signature)}`).toBe(vector.certificate);
      });
    }

    it(`checks as marked: ${vector.name}`, async () => {
      const result = await checkCertificate(envFor(), vector.certificate);
      if (vector.valid) {
        expect(result.status).toBe('valid');
      } else if (vector.reason === 'unknown_key') {
        expect(result.status).toBe('unverifiable');
      } else {
        expect(result.status).toBe('forged');
      }
    });
  }

  for (const job of vectors.jobs) {
    it(`job canonical JSON: ${job.name}`, () => {
      expect(canonicalJson(job.payload)).toBe(job.canonical);
    });
    it(`job re-signs byte-for-byte with the delegation seed: ${job.name}`, async () => {
      const { body, signature } = await signPayload(base64urlDecode(vectors.delegation_seed), job.payload);
      expect(`PLEXORAD1.${base64url(body)}.${base64url(signature)}`).toBe(job.certificate);
    });
  }

  it('grants the same Paid default as the client manifest', () => {
    expect(PAID_ENTITLEMENTS).toEqual(vectors.paid_entitlements);
  });
});

describe('canonical JSON', () => {
  it('refuses non-integer numbers', () => {
    expect(() => canonicalJson({ a: 1.5 })).toThrow();
  });
  it('sorts keys at every depth and drops undefined', () => {
    expect(canonicalJson({ b: { d: 1, c: [2, { f: 1, e: 0 }] }, a: null, z: undefined })).toBe(
      '{"a":null,"b":{"c":[2,{"e":0,"f":1}],"d":1}}');
  });
});
