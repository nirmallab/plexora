/**
 * The signer's own rules: slot-strict kids, the three check outcomes, and
 * credentials that look the way the client expects.
 */
import { describe, expect, it } from 'vitest';

import { type CertificatePayload, checkCertificate, signCertificate, signingConfigured } from '../src/certs';
import { base64url, LICENSE_TOKEN_PATTERN, newLicenseToken, newSeatKey, normalizeSeatKey, SEAT_KEY_PATTERN } from '../src/crypto';
import type { Env } from '../src/env';

const seed = base64url(new Uint8Array(32).map((_, i) => i + 1));

function payload(kid = 'px1'): CertificatePayload {
  return {
    v: 1, aud: 'plexora', kid, cert_id: 'crt_x', license_id: 'lic_x', account_id: 'acc_x', seat_id: 'sa_x',
    plan: 'paid', trial: false, use_class: 'academic', entitlements: ['ai'], environment_id: 'env_x',
    environment_type: 'desktop', env_binding: 'a'.repeat(64), delegation_pubkey: null, issued_at: 1, expires_at: 2,
    grace_days: 14, offline_until: null,
  };
}

describe('signing', () => {
  it('signs with the slot named by the kid, and checks it back', async () => {
    const env = { SIGNING_KEY_PX1: seed, ACTIVE_KID: 'px1' } as unknown as Env;
    expect(signingConfigured(env)).toBe(true);
    const cert = await signCertificate(env, payload());
    expect(cert.startsWith('PLEXORA1.px1.')).toBe(true);
    const checked = await checkCertificate(env, cert);
    expect(checked.status).toBe('valid');
  });

  it('refuses to sign px2 with px1\'s key (slot-strict)', async () => {
    const env = { SIGNING_KEY_PX1: seed, ACTIVE_KID: 'px2' } as unknown as Env;
    expect(signingConfigured(env)).toBe(false);
    await expect(signCertificate(env, payload('px2'))).rejects.toThrow(/no signing key/);
  });

  it('keeps a retired kid verifiable through PUBLIC_KEYS_JSON', async () => {
    const signer = { SIGNING_KEY_PX1: seed, ACTIVE_KID: 'px1' } as unknown as Env;
    const cert = await signCertificate(signer, payload());
    const { publicKeyOfSeed } = await import('../src/certs');
    const { base64urlDecode } = await import('../src/crypto');
    const pub = await publicKeyOfSeed(base64urlDecode(seed));
    const verifierOnly = { PUBLIC_KEYS_JSON: JSON.stringify({ px1: pub }) } as unknown as Env;
    expect((await checkCertificate(verifierOnly, cert)).status).toBe('valid');
  });

  it('separates forged from unverifiable', async () => {
    const env = { SIGNING_KEY_PX1: seed, ACTIVE_KID: 'px1' } as unknown as Env;
    const cert = await signCertificate(env, payload());
    const [prefix, kid, body, sig] = cert.split('.');
    expect((await checkCertificate(env, `${prefix}.${kid}.${body}.${sig!.slice(0, -4)}AAAA`)).status).toBe('forged');
    expect((await checkCertificate(env, 'nonsense')).status).toBe('forged');
    expect((await checkCertificate({} as Env, cert)).status).toBe('unverifiable');
  });

  it('refuses a certificate for another product even when the signature is ours', async () => {
    const env = { SIGNING_KEY_PX1: seed, ACTIVE_KID: 'px1' } as unknown as Env;
    const other = { ...payload(), aud: 'scimappro' } as unknown as CertificatePayload;
    const cert = await signCertificate(env, other);
    expect(await checkCertificate(env, cert)).toEqual({ status: 'forged', reason: 'not a Plexora certificate' });
  });
});

describe('credentials', () => {
  it('seat keys are unambiguous and normalise', () => {
    for (let i = 0; i < 50; i += 1) {
      const key = newSeatKey();
      expect(key).toMatch(SEAT_KEY_PATTERN);
      // No characters that read as one another (0/O, 1/I/L).
      expect(key.slice(5)).not.toMatch(/[01ILO]/);
      expect(normalizeSeatKey(key.toLowerCase().replace(/-/g, ' '))).toBe(key);
    }
  });
  it('licence tokens have the client\'s shape', () => {
    expect(newLicenseToken()).toMatch(LICENSE_TOKEN_PATTERN);
  });
});
