/**
 * The certificate check against the cross-language vectors the `biocognia` package and the platform test with
 * (vectors/biocognia-vectors.json, the file tests/js/license_probe.mjs runs through licenseVerify.js), and the relay's
 * trusted keys against the website's.
 *
 * The vectors come from BIOCOGNIA_VECTORS, else the repository's .venv (the installed `biocognia` package); without
 * either the vector tests are skipped, and say so. Certificates minted here with a fresh key cover expiry and grace.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, describe, expect, it } from 'vitest';

import { checkLicence, licensedUntil, PUBLIC_KEYS, setTrustedKeysForTests, verify } from '../src/licence';
import { mintKey } from './fixtures';

const REPO = fileURLToPath(new URL('../..', import.meta.url));

function findVectors(): string | null {
  if (process.env.BIOCOGNIA_VECTORS) return process.env.BIOCOGNIA_VECTORS;
  const lib = join(REPO, '.venv', 'lib');
  if (!existsSync(lib)) return null;
  for (const python of readdirSync(lib)) {
    const candidate = join(lib, python, 'site-packages', 'biocognia', 'vectors', 'biocognia-vectors.json');
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

interface Vector {
  name: string;
  audience: string;
  certificate: string;
  expect: string;
}

const vectorsPath = findVectors();
const vectors = vectorsPath
  ? (JSON.parse(readFileSync(vectorsPath, 'utf8')) as { kid: string; public_key: string; verify_at: number; certificates: Vector[] })
  : null;

afterEach(() => setTrustedKeysForTests(null));

describe.skipIf(!vectors)(`the platform's certificate vectors (${vectorsPath ?? 'not found: set BIOCOGNIA_VECTORS'})`, () => {
  it('every vector reads as the package reads it', async () => {
    const v = vectors!;
    const keys = { [v.kid]: v.public_key };
    for (const vector of v.certificates) {
      let outcome = 'valid';
      try {
        await verify(vector.certificate, { audience: vector.audience, now: v.verify_at, keys });
      } catch (error) {
        outcome = (error as { code?: string }).code ?? 'thrown';
      }
      expect(outcome, vector.name).toBe(vector.expect);
    }
  });

  it('the gate takes the plexora vector and refuses the others for plexora', async () => {
    const v = vectors!;
    setTrustedKeysForTests({ [v.kid]: v.public_key });
    const byName = Object.fromEntries(v.certificates.map((vector) => [vector.name, vector.certificate]));
    expect(await checkLicence(byName['plexora-desktop'] ?? '', v.verify_at)).toEqual({ ok: true });
    expect(await checkLicence(byName['scimappro-desktop'] ?? '', v.verify_at)).toMatchObject({ ok: false, code: 'wrong_product' });
    expect(await checkLicence(byName.tampered ?? '', v.verify_at)).toMatchObject({ ok: false, code: 'bad_signature' });
    // The production table does not trust the vectors' key.
    setTrustedKeysForTests(null);
    expect(await checkLicence(byName['plexora-desktop'] ?? '', v.verify_at)).toMatchObject({ ok: false, code: 'unknown_key' });
  });
});

describe('the relay trusts exactly the website\'s keys', () => {
  it('PUBLIC_KEYS equals licenseVerify.js PUBLIC_KEYS', () => {
    const source = readFileSync(join(REPO, 'plexora', 'client', 'src', 'js', 'services', 'licenseVerify.js'), 'utf8');
    const block = /export const PUBLIC_KEYS = Object\.freeze\(\{([^}]*)\}\)/.exec(source)?.[1] ?? '';
    const website = Object.fromEntries([...block.matchAll(/(\w+):\s*'([A-Za-z0-9_-]+)'/g)].map((m) => [m[1], m[2]]));
    expect(Object.keys(website).length).toBeGreaterThan(0);
    expect({ ...PUBLIC_KEYS }).toEqual(website);
  });
});

describe('expiry', () => {
  const key = mintKey('t9');
  const now = 2_000_000_000;
  const base = { aud: 'plexora', typ: 'env', iat: now - 86400 };

  it('the earlier of exp and offline_until, plus grace_days', () => {
    expect(licensedUntil({ exp: now })).toBe(now);
    expect(licensedUntil({ exp: now, grace_days: 2 })).toBe(now + 2 * 86400);
    expect(licensedUntil({ exp: now, offline_until: now - 10, grace_days: 0 })).toBe(now - 10);
    expect(licensedUntil({ offline_until: now })).toBe(now);
    expect(licensedUntil({})).toBeNull();
  });

  it('licensed until then, refused after; no certificate is refused', async () => {
    setTrustedKeysForTests({ t9: key.publicKey });
    const certificate = key.certify({ ...base, exp: now + 10, grace_days: 1 });
    expect(await checkLicence(certificate, now)).toEqual({ ok: true });
    expect(await checkLicence(certificate, now + 10 + 86400)).toEqual({ ok: true });
    expect(await checkLicence(certificate, now + 11 + 86400)).toMatchObject({ ok: false, code: 'expired' });
    expect(await checkLicence(key.certify({ ...base }), now)).toEqual({ ok: true });
    expect(await checkLicence(null, now)).toMatchObject({ ok: false, code: 'no_license' });
    expect(await checkLicence(key.certify({ ...base, iat: now + 3600 }), now)).toMatchObject({ ok: false, code: 'future_dated' });
    expect(await checkLicence(key.certify({ ...base, aud: 'scimappro' }), now)).toMatchObject({ ok: false, code: 'wrong_product' });
    expect(await checkLicence(key.certify({ ...base, v: 2 }), now)).toMatchObject({ ok: false, code: 'unsupported_version' });
  });
});
